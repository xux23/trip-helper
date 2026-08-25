"""CLI 入口：输入 → 编排 → 输出文本行程单 → 人工调整循环（FR7）。

调整只触发 Budget 重算（必要时用已有候选补充），不重跑全流程、不调 LLM。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path


def _ensure_runtime() -> None:
    """一键启动自举：若当前解释器缺少依赖（如系统 python），自动改用项目
    .venv 或 uv run 重新执行本文件，使 `python main.py` / 双击可直接运行。"""
    try:
        import pydantic  # noqa: F401

        return
    except ImportError:
        pass
    here = Path(__file__).resolve()
    project_root = here.parents[2]
    for cand in (
        project_root / ".venv" / "Scripts" / "python.exe",
        project_root / ".venv" / "bin" / "python",
    ):
        if cand.exists():
            os.execv(str(cand), [str(cand), str(here), *sys.argv[1:]])
    uv = shutil.which("uv")
    if uv:
        os.execv(uv, [uv, "run", "python", str(here), *sys.argv[1:]])


import shutil  # noqa: E402  (在 _ensure_runtime 使用，延迟到此处导入)

_ensure_runtime()

# 直接 `python src/travel_planner/main.py` 时，把包根目录（src）加入 sys.path，
# 使 travel_planner.* 绝对导入可用。
_SRC_ROOT = str(Path(__file__).resolve().parents[1])
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)

from travel_planner import config
from travel_planner.agents.budget import BudgetAgent
from travel_planner.llm import make_llm_client
from travel_planner.orchestrator import PlanEvent, PlanState, UnclearRequest, stream_plan
from travel_planner.tools.amap import QuotaExhaustedError, QuotaTracker

WEATHER_ICON = {"晴": "☀", "多云": "⛅", "阴": "☁", "小雨": "🌦", "大雨": "🌧"}

BREAKDOWN_LABELS = {
    "transport_intercity": "城际交通",
    "transport_city": "市内交通",
    "lodging": "住宿",
    "food": "餐饮",
    "tickets": "门票",
    "misc": "机动预留",
}
STATUS_LABELS = {"ok": "预算合适", "over": "预算超标", "under": "预算宽裕"}


def render_itinerary(state: PlanState) -> str:
    it, report = state.itinerary, state.report
    student = bool(state.request.student)

    def meal_txt(r) -> str:
        if student and r.groupon_price is not None and r.groupon_price < r.price_per_person:
            return f"（团购人均 {r.groupon_price} 元，{r.area}）"
        return f"（人均 {r.price_per_person} 元，{r.area}）"

    lines: list[str] = []
    lines.append("=" * 56)
    lines.append(f"✈ {it.destination} {len(it.days)} 日行程单")
    lines.append("=" * 56)
    lines.append(
        f"🏨 酒店：{it.hotel.name}（{it.hotel.tier}，{it.hotel.price_per_night} 元/晚，"
        f"{it.hotel.area}，住 {max(len(it.days) - 1, 0)} 晚）"
    )
    lines.append("")

    for day in it.days:
        w = day.weather
        weather_txt = (
            f"{WEATHER_ICON.get(w.condition, '')}{w.condition} {w.temp_low}~{w.temp_high}℃"
            if w
            else "天气未知"
        )
        date_txt = day.date or "日期未定"
        lines.append(f"—— 第 {day.day} 天 · {date_txt} · {weather_txt} ——")
        lines.append(f"  上午　{describe_attraction(day.morning.attraction, student)}")
        lines.append(f"  下午　{describe_attraction(day.afternoon.attraction, student)}")
        evening = day.evening
        if evening.attraction is not None:
            lines.append(f"  晚上　{describe_attraction(evening.attraction, student)}")
        elif evening.restaurant is not None:
            r = evening.restaurant
            lines.append(
                f"  晚上　夜市：{r.name}（人均 {r.price_per_person} 元）"
            )
        else:
            lines.append(f"  晚上　{evening.text}")
        lunch, dinner = day.meals.lunch, day.meals.dinner
        lines.append(f"  午餐　{lunch.name}{meal_txt(lunch)}")
        lines.append(f"  晚餐　{dinner.name}{meal_txt(dinner)}")
        lines.append("")

    lines.extend(render_report(report))
    notes: list[str] = []
    if student:
        notes.append("已按学生票（景点约半价）与团购价（餐饮约 8.5 折）估算")
    if any(r.source == "fallback" for r in state.results):
        notes.append("部分数据来自兜底数据源（source=fallback）")
    if it.meta.degraded:
        notes.append("本次为降级生成（LLM 编排失败，已用代码模板编排）")
    if it.meta.adjust_rounds:
        notes.append(f"已自动执行 {it.meta.adjust_rounds} 轮预算调整")
    for note in notes:
        lines.append(f"* {note}")
    return "\n".join(lines)


def describe_attraction(a, student: bool = False) -> str:
    if student and a.student_price is not None:
        price = f"学生票 {a.student_price} 元（原价 {a.price}）"
    elif a.price:
        price = f"{a.price} 元门票"
    else:
        price = "免费"
    tags = "/".join(a.tags[:2])
    return f"{a.name}（{price}，约 {a.duration_hours:g} 小时，{a.area}，{tags}）"


def render_report(report) -> list[str]:
    lines = ["—— 预算报告 ——"]
    for key in BREAKDOWN_LABELS:
        lines.append(f"  {BREAKDOWN_LABELS[key]}：{report.breakdown.get(key, 0)} 元")
    lines.append(f"  合计：{report.estimated_total} 元 / 预算 {report.budget} 元")
    lines.append(f"  状态：{STATUS_LABELS[report.status]}")
    for s in report.suggestions:
        if s.action == "none":
            continue
        detail = s.reason or ""
        if s.action == "downgrade_hotel":
            detail = f"酒店降档 {s.from_tier} → {s.to_tier}（{detail}）"
        elif s.action == "replace_paid_attraction":
            detail = f"{s.target}：{detail}"
        elif s.action == "reduce_daily_activities":
            detail = f"压缩每日景点（{detail}）"
        lines.append(f"  建议：{detail}")
    if report.note:
        lines.append(f"  备注：{report.note}")
    return lines


def stream_print(text: str) -> None:
    """逐行流式打印，每行立即刷新，营造边生成边输出的效果。"""
    for line in text.splitlines():
        print(line)
        sys.stdout.flush()


# ============================================================================
# 思考动画：等待 LLM / 高德查询期间转圈 + 实时耗时；完成阶段打勾并记录用时
# ============================================================================

SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

_spinner_task: asyncio.Task | None = None
_spinner_label = ""
_spinner_start = 0.0


def _start_thinking(label: str) -> None:
    """启动/更新"思考中"动画（后台任务在同一行重绘）。重复调用只换文案和计时起点。"""
    global _spinner_task, _spinner_label, _spinner_start
    _spinner_label = label
    _spinner_start = time.monotonic()
    if _spinner_task is None or _spinner_task.done():
        _spinner_task = asyncio.create_task(_spin())


def _resume_thinking() -> None:
    """动画被临时打断（如打印 ℹ 提示）后恢复，计时不中断。"""
    global _spinner_task
    if _spinner_task is None or _spinner_task.done():
        _spinner_task = asyncio.create_task(_spin())


async def _spin() -> None:
    i = 0
    try:
        while True:
            elapsed = time.monotonic() - _spinner_start
            sys.stdout.write(f"\r{SPINNER_FRAMES[i]} {_spinner_label} {elapsed:.1f}s")
            sys.stdout.flush()
            i = (i + 1) % len(SPINNER_FRAMES)
            await asyncio.sleep(0.1)
    except asyncio.CancelledError:
        pass


def _stop_thinking() -> float:
    """停止动画并清掉该行（同步，任何上下文可调），返回本次动画时长（秒）。"""
    global _spinner_task
    elapsed = time.monotonic() - _spinner_start if _spinner_task else 0.0
    if _spinner_task and not _spinner_task.done():
        _spinner_task.cancel()
    _spinner_task = None
    sys.stdout.write("\r" + " " * 88 + "\r")
    sys.stdout.flush()
    return elapsed


def _print_line(text: str) -> None:
    """清掉动画行后整行打印（打印前必须清，否则与动画行混在一起）。"""
    _stop_thinking()
    stream_print(text)


def _finalize_stage(label: str | None) -> None:
    """一个阶段完成：清动画 → 打勾并记录该阶段耗时。"""
    if not label:
        return
    elapsed = _stop_thinking()
    print(f"✓ {label}（{elapsed:.1f}s）")
    sys.stdout.flush()


def render_search_list(
    results, city: str, keyword: str, source: str, student: bool = False, web_hits: list | None = None
) -> str:
    """搜索模式结果渲染：名称（免费/学生票/估算价，区域，评分，标签）+ 网上攻略摘录。"""
    lines = [f"🔎 {city}「{keyword or '景点'}」搜索结果（{len(results)} 条）"]
    if source == "fallback":
        lines.append("* 高德在线查询失败，以下为通用兜底数据")
    for i, a in enumerate(results, 1):
        if a.price == 0:
            price = "免费"
        elif student and a.student_price is not None:
            price = f"学生票 {a.student_price} 元"
        else:
            price = f"{a.price} 元（估算）"
        tags = "/".join(a.tags[:3]) or "无标签"
        area = a.area or "区域未知"
        lines.append(f"{i:>2}. {a.name}（{price}，{area}，评分 {a.rating}，{tags}）")
    if web_hits:
        lines.append("")
        lines.append("💡 网上攻略摘录：")
        for h in web_hits[:2]:
            title = h.get("title") or ""
            snippet = (h.get("snippet") or "").strip()
            lines.append(f"  · {title}：{snippet}")
    return "\n".join(lines)


def _relatable_costs(amount: int) -> str:
    """把金额换算成普通人有体感的参照物（奶茶/火锅/电影票）。"""
    items = []
    if amount >= 15:
        items.append(f"{amount // 15} 杯奶茶")
    if amount >= 100:
        items.append(f"{amount // 100} 顿人均百元的火锅")
    if amount >= 50:
        items.append(f"{amount // 50} 张电影票")
    return "，或 ".join(items[:2]) if items else f"{amount} 元"


def render_budget_unfeasible(report, destination: str, days: int) -> str:
    """预算不现实：回环后仍超标，不硬排行程，给出最低可行预算与同类比价。"""
    minimum = report.estimated_total
    suggestion = round(minimum * 1.1 / 100) * 100  # 留 10% 余量、取整到百
    per_day = round(minimum / days) if days else minimum
    advice = [f"  1. 提高预算到 {suggestion} 元左右再试"]
    if days > 1:
        advice.append("  2. 减少游玩天数")
        advice.append("  3. 换个消费更低的目的地")
    else:  # 1 天行程没法再减天数
        advice.append("  2. 换个消费更低的目的地")
    return "\n".join([
        "⚠ 预算不现实，这次就不硬排行程了",
        f"你给的预算是人均 {report.budget} 元，但按省着玩的思路，{destination} {days} 天",
        f"最省也要约 {minimum} 元/人：",
        f"  · 相当于每天约 {per_day} 元（含住宿、餐饮、交通、门票）",
        f"  · 大约等于 {_relatable_costs(minimum)}",
        "",
        "建议：",
        *advice,
        "",
        f"（重新规划：在输入里说清新预算即可，例如「{destination}玩2天，预算{suggestion}」）",
    ])


def print_request_summary(request) -> None:
    prefs = "、".join(request.preferences) if request.preferences else "无特殊偏好"
    parts = [
        f"  ➤ 目的地：{request.destination}",
        f"  ➤ 天数：{request.days} 天",
        f"  ➤ 人均预算：{request.budget} 元",
        f"  ➤ 偏好：{prefs}",
    ]
    if request.party_size > 1:
        parts.append(f"  ➤ 人数：{request.party_size} 人")
    if request.student:
        parts.append("  ➤ 人群：学生（按学生票/团购价估算）")
    if request.departure_city:
        parts.append(f"  ➤ 出发城市：{request.departure_city}")
    if request.date:
        parts.append(f"  ➤ 日期：{request.date}")
    stream_print("\n".join(parts))


MENU = """
可用操作：
  1. 替换某天某时段的景点
  2. 更换酒店
  3. 修改预算
  4. 查看行程 JSON
  q. 完成并退出
请选择："""


class CLI:
    """input() 的薄封装，便于测试替换。

    - EOFError（stdin 关闭）→ 返回 None，由上层区分"普通回车"与"输入流已结束"；
    - KeyboardInterrupt 不吞掉，上抛后由 cli() 统一打印"已取消"。
    """

    def ask(self, prompt: str) -> str | None:
        try:
            return input(prompt).strip()
        except EOFError:
            return None

    def notify(self, message: str) -> None:
        _stop_thinking()  # 打印前先清动画行
        print(f"ℹ {message}")
        _resume_thinking()  # 恢复动画，计时不中断


def adjustment_loop(state: PlanState, io: CLI) -> None:
    """FR7：调整只触发 Budget 重算，不重跑全流程。"""
    budget_agent = BudgetAgent()
    planner = state.planner
    while True:
        choice = (io.ask(MENU) or "q").lower()
        try:
            if choice == "1":
                _adjust_attraction(state, planner, io, budget_agent)
            elif choice == "2":
                _adjust_hotel(state, planner, io, budget_agent)
            elif choice == "3":
                _adjust_budget(state, io, budget_agent)
            elif choice == "4":
                print(state.itinerary.model_dump_json(indent=2))
                print(state.report.model_dump_json(indent=2))
            elif choice in ("q", "quit", "退出"):
                print("祝您旅途愉快！")
                return
            else:
                io.notify("无效选项")
        except (ValueError,) as e:
            io.notify(str(e))


def _pick_number(io: CLI, prompt: str, upper: int) -> int | None:
    raw = io.ask(prompt) or ""
    if not raw.isdigit():
        return None
    n = int(raw)
    return n if 1 <= n <= upper else None


def _show_report(state: PlanState) -> None:
    state.report = BudgetAgent().check(state.request, state.itinerary)
    print("\n".join(render_report(state.report)))


def _adjust_attraction(state: PlanState, planner, io: CLI, budget_agent: BudgetAgent) -> None:
    days = state.itinerary.days
    day_no = _pick_number(io, f"要调整第几天？（1-{len(days)}）：", len(days))
    if day_no is None:
        raise ValueError("无效天数")
    day = next(d for d in days if d.day == day_no)
    slots = [("morning", "上午"), ("afternoon", "下午")]
    if day.evening.attraction is not None:
        slots.append(("evening", "晚上"))
    for i, (_, label) in enumerate(slots, 1):
        print(f"  {i}. {label}")
    slot_idx = _pick_number(io, "要调整哪个时段？：", len(slots))
    if slot_idx is None:
        raise ValueError("无效时段")
    slot_name = slots[slot_idx - 1][0]

    alternatives = planner.alternative_attractions(state.itinerary)
    if not alternatives:
        raise ValueError("候选池中没有可替换的景点了")
    for i, a in enumerate(alternatives, 1):
        print(f"  {i}. {describe_attraction(a, bool(state.request.student))}")
    pick = _pick_number(io, "选择新景点序号：", len(alternatives))
    if pick is None:
        raise ValueError("无效选择")

    state.itinerary = planner.replace_attraction(
        state.itinerary, day_no, slot_name, alternatives[pick - 1]
    )
    state.report = budget_agent.check(state.request, state.itinerary)
    print("\n".join(render_report(state.report)))


def _adjust_hotel(state: PlanState, planner, io: CLI, budget_agent: BudgetAgent) -> None:
    hotels = [h for h in planner.candidate_hotels() if h.name != state.itinerary.hotel.name]
    if not hotels:
        raise ValueError("候选池中没有其他酒店")
    for i, h in enumerate(hotels, 1):
        print(f"  {i}. {h.name}（{h.tier}，{h.price_per_night} 元/晚，{h.area}）")
    pick = _pick_number(io, "选择新酒店序号：", len(hotels))
    if pick is None:
        raise ValueError("无效选择")
    state.itinerary = planner.replace_hotel(state.itinerary, hotels[pick - 1])
    state.report = budget_agent.check(state.request, state.itinerary)
    print("\n".join(render_report(state.report)))


def _adjust_budget(state: PlanState, io: CLI, budget_agent: BudgetAgent) -> None:
    raw = io.ask(f"请输入新的人均预算（当前 {state.request.budget} 元）：") or ""
    value = int("".join(ch for ch in raw if ch.isdigit()) or 0)
    if value <= 0:
        raise ValueError("预算须为正整数")
    state.request.budget = value
    state.report = budget_agent.check(state.request, state.itinerary)
    print("\n".join(render_report(state.report)))


async def _amain(argv: list[str]) -> int:
    llm = make_llm_client()
    if llm is None:
        print(
            "未配置 LLM。请设置环境变量后重试：\n"
            "  export LLM_BASE_URL=...   # OpenAI 兼容服务地址\n"
            "  export LLM_API_KEY=...\n"
            "  export LLM_MODEL=..."
        )
        return 1

    # 高德在线数据源：无 Key 打警告继续（走通用兜底）；配额耗尽则直接停止
    if not config.AMAP_KEY:
        print("⚠ 未配置 AMAP_KEY，将使用通用兜底数据，推荐效果有限。请在 .env 添加高德 Key。")
    else:
        exhausted = QuotaTracker(config.AMAP_QUOTA_FILE, config.amap_daily_limits()).exhausted_services()
        if exhausted:
            print("高德免费配额已用完（今日超限服务：" + "、".join(exhausted) + "），程序停止。")
            print("请明天再试，或在 .env 调整 AMAP_DAILY_LIMIT_* 后重启。")
            return 3

    print("=" * 56)
    print("智能旅行规划助手 · 一句话输入，得到完整行程单")
    print('示例："国庆去成都玩3天，预算3000，喜欢美食" 或 "找成都免费景点"')
    print("=" * 56)

    if argv:
        user_input = " ".join(argv)
    else:
        # 空输入不直接退出：最多重问 3 次（防误触回车/终端偶发空行）；
        # stdin 已关闭（EOF）则明确提示后退出。
        user_input = ""
        for _ in range(3):
            answer = CLI().ask("说说你想怎么玩？")
            if answer is None:
                print("没有收到输入，程序退出。")
                return 1
            user_input = answer.strip()
            if user_input:
                break
            print("没听清，再说说？")
    if not user_input:
        print("好的，那下次再玩 👋")
        return 0

    io = CLI()
    state: PlanState | None = None
    pending_stage: str | None = None
    try:
        async for event in stream_plan(user_input, llm, io):
            if event.stage == "stage":
                _finalize_stage(pending_stage)  # 上一条打勾并记录耗时
                pending_stage = event.payload
                _start_thinking(pending_stage)  # 当前阶段转圈 + 实时耗时
            elif event.stage == "request":
                _finalize_stage(pending_stage)
                pending_stage = None
                print_request_summary(event.payload)
            elif event.stage == "done":
                _finalize_stage(pending_stage)
                pending_stage = None
                _stop_thinking()
                state = event.payload
    except UnclearRequest as e:  # 需求不清晰且追问无果：不开始安排
        _stop_thinking()
        print(f"\n{e}")
        return 1
    except QuotaExhaustedError as e:  # 配额耗尽：停止整个程序（用户约定）
        _stop_thinking()
        print(f"\n{e}")
        print("高德免费配额已用完，程序停止。请明天再试，或在 .env 调整 AMAP_DAILY_LIMIT_* 后重启。")
        return 3
    except Exception as e:  # 文档第 9 章：LLM 故障给出可操作的提示
        _stop_thinking()
        print(f"规划失败：{e}")
        print("请检查 LLM_BASE_URL / LLM_API_KEY 配置与网络连接后重试。")
        return 1

    if state is None:
        print("未能生成行程")
        return 1

    if state.search_results is not None:  # 搜索模式：渲染结果列表，无调整循环
        stream_print(
            render_search_list(
                state.search_results,
                state.request.destination,
                state.request.search_keyword,
                state.search_source,
                student=bool(state.request.student),
                web_hits=state.web_hits,
            )
        )
        return 0

    if state.report is not None and state.report.status == "over":
        # 预算不现实：不输出强行安排的行程，改为提示（回环后仍超标）
        stream_print(
            render_budget_unfeasible(state.report, state.request.destination, state.request.days)
        )
        return 0

    print()
    stream_print(render_itinerary(state))
    adjustment_loop(state, io)
    return 0


def cli() -> None:
    try:
        sys.exit(asyncio.run(_amain(sys.argv[1:])))
    except KeyboardInterrupt:
        # Ctrl+C 时干净退出，不把 asyncio 内部的 CancelledError 栈打印给用户
        _stop_thinking()
        print("\n已取消")
        sys.exit(130)


if __name__ == "__main__":
    cli()
