# 智能旅行规划助手（trip-helper）

一句话输入，输出结构完整、预算可控、可手动微调的旅行行程单。

3 个 Agent 顺序协作：**Planner（规划）→ Info（信息）→ Budget（预算）**，
Agent 之间用 JSON（pydantic 强校验）传结构化数据，预算超标自动回环调整，
任何工具故障都有兜底数据，流程不中断。

> 设计依据 `docs/技术设计文档.md`，Prompt 定稿 `docs/prompt设计.md`。
> 全项目无 Agent 框架依赖，运行依赖仅 `openai` + `pydantic` + `httpx`。

## 快速开始

```bash
# 1. 安装（Python 3.12 + uv）
uv sync

# 2. 配置任意 OpenAI 兼容服务商 + 高德在线数据源
# 方式 A：写入项目根目录 .env（已纳入 .gitignore，不入库）
#   LLM_BASE_URL="https://apihub.agnes-ai.com/v1"
#   LLM_API_KEY="sk-..."
#   LLM_MODEL="agnes-2.5-flash"
#   AMAP_KEY="..."        # 高德开放平台免费个人 Key（在线景点/美食/酒店/天气）
# 方式 B：环境变量
export LLM_BASE_URL="https://apihub.agnes-ai.com/v1"
export LLM_API_KEY="sk-..."
export LLM_MODEL="agnes-2.5-flash"
export AMAP_KEY="..."

# 3. 启动 CLI
uv run travel-planner
```

示例输入：

```
国庆去成都玩3天，预算3000，喜欢美食     # → 完整行程单
找成都免费景点                          # → 在线搜索免费景点列表
```

输出文本行程单（每日上午/下午/晚上安排 + 午晚餐 + 酒店 + 分项预算报告），
随后进入人工调整循环：

- 替换某天某时段的景点（只触发预算重算，不重跑全流程）
- 更换酒店档次
- 修改预算
- 输出结构化 JSON 行程单

## 特性

| 能力 | 说明 |
|---|---|
| 在线数据源 | 高德 Web 服务 API：景点/美食/酒店/天气实时检索，任意城市可查；POI 按词表映射为 schema（门票/房价为分类估算，标注"价格估算"） |
| 输入即搜索 | "找成都免费景点" → 直接出在线搜索结果列表（免费/估算价 + 区域 + 评分），不走行程编排 |
| 免费网页搜索 | 搜索模式自动附带"💡 网上攻略摘录"（必应中国，免 Key；维基/DDG 可按网络配置启用）；失败不中断 |
| 免费配额保护 | 按服务每日限额（`AMAP_DAILY_LIMIT_*`），持久化计数；**超限整个程序停止**（exit 3），防超额计费 |
| 两处 LLM 调用 | 仅参数抽取 + 行程编排用 LLM；拆任务/算钱/调预算全是确定性代码 |
| 穷游取向 | 编排按"穷游但不穷玩"：能省则省（交通/住宿/非特色景点），特色美食与必打卡地标不省；预算偏紧默认经济型酒店 |
| 预算回环 | over 时按"降档酒店 → 正餐换平价特色店 → 换免费景点 → 压缩景点数"优先级自动调整，最多 2 轮；回环后仍超标 = 预算不现实，不硬排行程，提示最低可行预算与建议 |
| 三层兜底 | 高德失败（网络/超时/限流）→重试→通用兜底；LLM 输出非法**或网络故障**→重试→代码模板编排（降级不崩）；配额耗尽除外（直接停止） |
| 故障注入 | `SIMULATE_TOOL_FAILURE=1` 时工具层 50% 概率抛错，现场演示降级 |
| 信封日志 | `LOG_ENVELOPES=1` 打印 Agent 间 JSON 消息信封 |
| 即时交互 | `openai` 惰性导入：启动到横幅秒出；等待期间转圈动画 + 实时耗时，每阶段完成打勾记录用时；任何环节 Ctrl+C 干净退出 |

## 测试与评估

```bash
uv run pytest                 # 60 个单测：schema/工具/预算/Planner/编排/搜索/配额/CLI 全链路（离线）

uv run python tests/eval.py --limit 5   # 快速评估试跑（需 LLM Key）
uv run python tests/eval.py             # 全量 50 用例：基线 vs 多 Agent 对比
```

评估按文档 11.2 的 10 项 checklist 自动打分（≥7 分记合理），产出 `docs/评估报告.md`
（合理率 / 耗时 / LLM 调用次数与 token 成本对比 + 失败案例分析）。

## 目录结构

```
src/travel_planner/
├── main.py            # CLI 入口：行程单渲染 + 搜索列表 + FR7 人工调整循环 + 配额启动检查
├── orchestrator.py    # 顺序编排 + 搜索分支 + 预算回环（≈40 行主流程）
├── schemas.py         # 第 5 章全部 schema 的 pydantic 定义
├── config.py          # 环境变量（LLM 三件套 + AMAP 配置/配额）+ 全部规则常量集中区
├── llm.py             # OpenAI 兼容客户端（惰性导入 + 网络重试）
├── agents/
│   ├── planner.py     # 抽取 / 拆任务 / 编排（降级模板）/ 执行调整
│   ├── info.py        # 工具调用：超时→重试→兜底；搜索；配额异常上抛
│   └── budget.py      # 分项估算 / 判定 / 建议（纯代码）
└── tools/
    ├── amap.py        # 高德客户端：QuotaTracker / 搜索 / 天气 / POI→schema 映射
    ├── search.py      # 在线景点搜索（含免费过滤）
    ├── poi.py food.py hotel.py weather.py   # 统一异步接口的在线工具
    ├── loader.py      # 通用兜底数据 + 故障注入
    └── data/fallback.json   # 通用兜底（离线城市数据已移除）
tests/
├── test_*.py          # 单测（离线可跑，工具层用 FakeAmap 注入）
├── eval_cases.json    # 50 条评估用例（10 城 × 5 形态）
└── eval.py            # 基线 vs 多 Agent 对比评估
docs/
├── 技术设计文档.md    # 唯一设计依据
├── prompt设计.md      # 两处 LLM Prompt 定稿
└── 面试问答要点.md
```

## 设计要点（为什么这么做）

- **不用 LangChain**：编排器手写约 40 行，每行都能讲清楚；框架抽象比业务还复杂。
- **JSON 而非自然语言通信**：pydantic 校验让失败路径清晰（要么合法要么触发重试）。
- **真实在线数据但统一 API 形状**：工具层异步、可超时、可失败、有配额保护；换数据源只改 `tools/amap.py` 的映射函数。
- **配额是硬约束**：免费 Key 超限会花钱，宁可停掉整个程序也不静默超额调用。
- **回环零 LLM**：调整轮只做确定性替换 + 复检，耗时成本近乎为零。

详细取舍见 `docs/技术设计文档.md` 与 `docs/面试问答要点.md`。
