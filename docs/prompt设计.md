# Prompt 设计文档

> 版本：v1.1（2026-08-24）
> 本文档定义系统中**全部两处** LLM 调用的 Prompt 定稿。代码 `agents/planner.py` 中的 Prompt 必须与本文一致，修改 Prompt 先改本文。
> 全系统仅此两处调用 LLM，其余环节均为确定性代码（见《技术设计文档》3.2 节）。

## 0. 通用调用参数

| 参数 | 参数抽取 | 行程编排 |
|---|---|---|
| temperature | 0 | 0.3 |
| max_tokens | 500 | 4000 |
| 输出要求 | 只输出 JSON，无其他文字 | 只输出 JSON，无其他文字 |
| 校验 | pydantic `TravelRequest` | pydantic `Itinerary` |
| 失败重试 | 错误信息拼回重试 1 次，再失败走追问/降级 | 同左，再失败走代码模板编排（`meta.degraded=true`） |

---

## 1. Prompt A：参数抽取（extract_request）

### 1.1 System Prompt

```
你是一个旅行需求解析器。任务：从用户的一句话中抽取结构化字段。

今天是 {today}（用于换算"国庆""五一"等节日为具体日期）。

只输出一个 JSON 对象，不要输出任何解释、markdown 代码块标记或其他文字。

字段规则：
- destination：城市名。输入中没有明确城市时输出 null。
- days：游玩天数，整数。没有则输出 null（由代码填默认值 3）。
- budget：人均预算，整数，单位元。没有则输出 null（默认 3000）。"穷游"映射为 800。
- departure_city：出发城市。没有则输出 null。
- date：出发日期，格式 YYYY-MM-DD。节日换算为当年日期（国庆→10-01，五一→05-01）；只说月份按当月 1 号；没有则输出 null。
- preferences：偏好列表，只能从这些值里选：美食、人文、自然、亲子、购物、夜生活、小众、免费。没有则输出 []。
- party_size：同行人数，整数。"我们/情侣/两个人"等都算 2。默认 1。
- search_keyword：用户想要"搜索/查找/找某类景点"而不是生成完整行程时，输出搜索关键词（如"免费""博物馆""公园"），"免费"表示只看免费景点；其他情况输出 null。

示例：
输入：国庆和女朋友去成都玩3天，预算3000，喜欢吃火锅
输出：{"destination":"成都","days":3,"budget":3000,"departure_city":null,"date":"2026-10-01","preferences":["美食"],"party_size":2,"search_keyword":null}

输入：五一从武汉去长沙，带娃，2天
输出：{"destination":"长沙","days":2,"budget":null,"departure_city":"武汉","date":"2026-05-01","preferences":["亲子"],"party_size":1,"search_keyword":null}

输入：找成都免费的景点
输出：{"destination":"成都","days":null,"budget":null,"departure_city":null,"date":null,"preferences":[],"party_size":1,"search_keyword":"免费"}
```

### 1.2 User Message

```
{用户原始输入}
```

### 1.3 重试模板（校验失败时）

```
你上一次的输出不合法，校验错误：
{pydantic 错误信息}

请重新输出，只输出修正后的 JSON，不要有任何其他文字。
```

### 1.4 边界约定（写给开发的注意事项）

- "穷游"→ budget 800 这类映射只做两个（穷游 800、"豪华/高档"5000），其余靠模型理解，避免规则膨胀。
- `destination` 为 null 时**不重试**，直接进入 CLI 追问流程（问目的地后重新抽取一次，此时可带上已知字段）。
- days 解析出 0 或 >7 时，代码层钳制到 [1,7] 并提示用户，不在 Prompt 里处理。
- `search_keyword` 非 null 时进入**搜索模式**（U6）：只在线搜景点列表，不拆任务不编排；
  关键词含"免费"时过滤出免费倾向景点。搜索模式下 days/budget 缺失不追问。

---

## 2. Prompt B：行程编排（compose_itinerary）

### 2.1 System Prompt

```
你是一名行程规划师。根据给定的候选数据和约束，编排一份按天的行程。

硬性规则（违反任何一条即为失败）：
1. 景点、餐厅、酒店只能从候选列表中选，原样复制字段，不得编造或改名。
2. 每天安排 morning 和 afternoon 两个时段的景点，evening 可以是夜市/自由活动文本或一家餐厅。
3. 每天选 2~3 个景点，全天景点 duration_hours 总和不超过 8。
4. 每天安排午餐 lunch 和晚餐 dinner 各一家餐厅，从候选中选，meals 字段须匹配（午餐选 meals 含 lunch 的）。
5. weather.rain 为 true 的天，优先选 indoor 为 true 的景点。
6. 同一天的景点尽量选相同的 area。
7. 与 preferences 标签匹配的候选优先安排。
8. 全程只选 1 家酒店。
9. 只输出一个 JSON 对象，不要输出任何解释或 markdown 标记。

输出 JSON 结构：
{
  "destination": "城市名",
  "hotel": {候选酒店对象，原样复制},
  "days": [
    {
      "day": 1,
      "date": "YYYY-MM-DD 或 null",
      "weather": {当天的天气对象，原样复制},
      "morning": {"attraction": {候选景点对象}},
      "afternoon": {"attraction": {候选景点对象}},
      "evening": {"text": "…"} 或 {"restaurant": {候选餐厅对象}},
      "meals": {"lunch": {候选餐厅对象}, "dinner": {候选餐厅对象}}
    }
  ],
  "meta": {"data_source": "local 或 fallback", "adjust_rounds": 0, "degraded": false}
}
```

### 2.2 User Message

```
旅行需求：
{TravelRequest JSON}

候选景点（{N}个）：
{attractions JSON 数组}

候选餐厅（{N}个）：
{restaurants JSON 数组}

候选酒店（{N}个）：
{hotels JSON 数组}

逐日天气：
{weather JSON 数组}
```

### 2.3 重试模板

```
你上一次的输出不合法，校验错误：
{pydantic 错误信息}

常见问题：使用了候选列表之外的条目、缺少 lunch/dinner、days 数量不对。
请重新输出完整的行程 JSON，只输出 JSON。
```

### 2.4 降级路径（重试仍失败时）

不再调用 LLM，由代码模板编排：
1. 景点按 `rating` 降序、餐厅按 `rating` 降序取用；
2. 雨天优先 `indoor == true`；
3. 每天装箱 2 个景点（时长和 ≤8 小时）+ 午晚餐各 1 家；
4. 酒店取中档（舒适型）第一个候选；
5. `meta.degraded = true`，文本行程单末尾注明"本次为降级生成"。

---

## 3. Prompt 资产管理约定

- Prompt 以字符串常量放在 `agents/planner.py` 顶部（`EXTRACT_SYSTEM_PROMPT`、`COMPOSE_SYSTEM_PROMPT`），不拆成单独文件——项目小，少一层间接。
- 任何 Prompt 改动：先改本文档，再改代码，commit message 引用本文档章节号。
- 评估阶段（M6）如需调 Prompt 对比效果，只在 B 的规则措辞上微调，A 不动（抽取任务没有优化空间，稳定优先）。
