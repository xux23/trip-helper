"""CLI 入口：输入 → 编排 → 输出文本行程单 → 人工调整循环（FR7）。

调整只触发 Budget 重算（必要时用已有候选补充），不重跑全流程、不调 LLM。
"""

from __future__ import annotations

import asyncio
import sys

from travel_planner.agents.budget import BudgetAgent
from travel_planner.llm import make_llm_client
from travel_planner.orchestrator import PlanState, plan

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
        lines.append(f"  上午　{describe_attraction(day.morning.attraction)}")
        lines.append(f"  下午　{describe_attraction(day.afternoon.attraction)}")
        evening = day.evening
        if evening.attraction is not None:
            lines.append(f"  晚上　{describe_attraction(evening.attraction)}")
        elif evening.restaurant is not None:
            r = evening.restaurant
            lines.append(
                f"  晚上　夜市：{r.name}（人均 {r.price_per_person} 元）"
            )
        else:
            lines.append(f"  晚上　{evening.text}")
        lunch, dinner = day.meals.lunch, day.meals.dinner
        lines.append(f"  午餐　{lunch.name}（人均 {lunch.price_per_person} 元，{lunch.area}）")
        lines.append(f"  晚餐　{dinner.name}（人均 {dinner.price_per_person} 元，{dinner.area}）")
        lines.append("")

    lines.extend(render_report(report))
    notes: list[str] = []
    if any(r.source == "fallback" for r in state.results):
        notes.append("部分数据来自兜底数据源（source=fallback）")
    if it.meta.degraded:
        notes.append("本次为降级生成（LLM 编排失败，已用代码模板编排）")
    if it.meta.adjust_rounds:
        notes.append(f"已自动执行 {it.meta.adjust_rounds} 轮预算调整")
    for note in notes:
        lines.append(f"* {note}")
    return "\n".join(lines)


def describe_attraction(a) -> str:
    price = f"{a.price} 元门票" if a.price else "免费"
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


MENU = """
可用操作：
  1. 替换某天某时段的景点
  2. 更换酒店
  3. 修改预算
  4. 查看行程 JSON
  q. 完成并退出
请选择："""


class CLI:
    """input() 的薄封装，便于测试替换。"""

    def ask(self, prompt: str) -> str | None:
        try:
            return input(prompt).strip()
        except EOFError:
            return None

    def notify(self, message: str) -> None:
        print(f"ℹ {message}")


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
        print(f"  {i}. {describe_attraction(a)}")
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

    print("=" * 56)
    print("智能旅行规划助手 · 一句话输入，得到完整行程单")
    print('示例："国庆去成都玩3天，预算3000，喜欢美食"')
    print("=" * 56)

    if argv:
        user_input = " ".join(argv)
    else:
        user_input = (CLI().ask("您的旅行需求：") or "").strip()
    if not user_input:
        print("需求不能为空")
        return 1

    io = CLI()
    try:
        state = await plan(user_input, llm, io)
    except Exception as e:  # 文档第 9 章：LLM 故障给出可操作的提示
        print(f"规划失败：{e}")
        print("请检查 LLM_BASE_URL / LLM_API_KEY 配置与网络连接后重试。")
        return 1

    print()
    print(render_itinerary(state))
    adjustment_loop(state, io)
    return 0


def cli() -> None:
    sys.exit(asyncio.run(_amain(sys.argv[1:])))


if __name__ == "__main__":
    cli()
