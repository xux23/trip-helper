"""顺序编排 + 预算回环（文档 3.3 节伪代码的实现）。

抽取 → 拆任务 → 取数 → 编排 → 算账 → 超标回环（最多 2 轮）→ 输出。
Agent 间消息用统一信封包裹并打日志（LOG_ENVELOPES=1 时输出到 stderr）。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from travel_planner import config
from travel_planner.agents.budget import BudgetAgent
from travel_planner.agents.info import InfoAgent
from travel_planner.agents.planner import (
    ExtractError,
    ExtractionOutcome,
    MissingDestination,
    Planner,
)
from travel_planner.llm import LLMClient
from travel_planner.schemas import Envelope


@dataclass
class PlanState:
    """一次规划的全部产物；人工调整循环（FR7）直接在这份状态上改。

    搜索模式（search_keyword 非空）只填 search_results / search_source，
    不产生 itinerary / report。
    """

    request: object
    planner: Planner  # 复用同一实例：候选池在编排时缓存于此
    subtasks: list | None = None
    results: list | None = None
    weather: list | None = None
    itinerary: object | None = None
    report: object | None = None
    search_results: list | None = None
    search_source: str | None = None


@dataclass
class PlanEvent:
    """流式输出事件：stage=进度提示文案，其余为各阶段产出。"""

    stage: str  # "stage" | "request" | "info" | "done"
    payload: Any = None


def _log(envelope: Envelope) -> None:
    if config.LOG_ENVELOPES:
        print(
            f"[{envelope.msg_type}] {envelope.sender}->{envelope.receiver}: {envelope.payload}",
            file=sys.stderr,
            flush=True,
        )


class SilentIO:
    """非交互 IO：追问一律走默认值，提示静默收集（评估脚本用）。"""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def ask(self, prompt: str) -> str | None:
        return ""

    def notify(self, message: str) -> None:
        self.messages.append(message)


def _notify(io, message: str) -> None:
    if io is not None:
        io.notify(message)


def _parse_int(raw: str | None) -> int | None:
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    return int(digits) if digits else None


async def stream_plan(user_input: str, llm: LLMClient, io=None) -> AsyncIterator[PlanEvent]:
    """流式编排：每完成一个阶段就 yield 一个事件，供 CLI 边算边输出。

    io 提供 ask(prompt)->str 与 notify(msg)；传 None 则全部走默认值（非交互）。
    最后一个事件为 ("done", PlanState)，包含全部产物用于人工调整循环。
    """
    ask = io.ask if io is not None else (lambda _p: "")
    planner = Planner(llm)
    info = InfoAgent()
    budget = BudgetAgent()

    yield PlanEvent("stage", "🔍 正在理解您的旅行需求…")

    # ① 需求理解：目的地缺失时追问一次，补全后重新抽取（FR1 / U3）
    try:
        outcome: ExtractionOutcome = await planner.extract_request(user_input)
    except MissingDestination as e:
        answer = (ask("没有识别到目的地，请问您想去哪个城市？") or "").strip()
        if not answer:
            raise ExtractError("缺少目的地，无法继续规划") from e
        outcome = await planner.re_extract_with_destination(user_input, answer)

    for notice in outcome.notices:
        _notify(io, notice)
    request = outcome.request
    yield PlanEvent("request", request)

    # ②' 搜索模式：只搜景点列表，不拆任务不编排（输入即搜索，U-搜索 用例）
    if request.search_keyword is not None:
        yield PlanEvent("stage", f"🔎 正在搜索 {request.destination} 的「{request.search_keyword}」景点…")
        attrs, source = await info.search(request.destination, request.search_keyword)
        state = PlanState(
            request=request,
            planner=planner,
            search_results=attrs,
            search_source=source,
        )
        yield PlanEvent("done", state)
        return

    # ② 缺失字段追问一次（天数/预算），带默认值；天数钳制在 [1,7]
    if "days" in outcome.missing_fields:
        raw = _parse_int(ask(f"游玩几天？（直接回车默认 {request.days} 天）"))
        request.days = min(max(raw, 1), 7) if raw else request.days
        _notify(io, f"本次行程按 {request.days} 天安排")
    if "budget" in outcome.missing_fields:
        raw = _parse_int(ask(f"人均预算多少元？（直接回车默认 {request.budget} 元）"))
        request.budget = raw if raw and raw > 0 else request.budget

    # ③ 子任务拆解：纯代码
    tasks = planner.make_subtasks(request)
    _log(
        Envelope(
            msg_type="subtasks",
            sender="planner",
            receiver="info",
            payload={"destination": request.destination, "tasks": [t.model_dump() for t in tasks]},
        )
    )

    yield PlanEvent("stage", f"📋 已规划至 {request.destination}，正在查询景点 / 美食 / 酒店…")

    # ④ 信息获取：超时→重试→兜底
    results, weather = await info.run(tasks, request)
    for r in results:
        _log(wrap_msg("info_result", "info", "planner", r))
    yield PlanEvent("info", results)

    yield PlanEvent("stage", "🗺 正在编排每日行程…")

    # ⑤ 行程编排：LLM（失败降级为代码模板）
    itinerary = await planner.compose_itinerary(request, results, weather)
    _log(wrap_msg("itinerary", "planner", "budget", itinerary))

    # ⑥ 预算校验 + 回环调整（回环内零 LLM，文档 8.3 节）
    report = budget.check(request, itinerary)
    rounds = 0
    while report.status == "over" and rounds < config.MAX_ADJUST_ROUNDS:
        itinerary = planner.apply_suggestions(itinerary, report.suggestions)
        rounds += 1
        itinerary.meta.adjust_rounds = rounds
        report = budget.check(request, itinerary)

    if report.status == "over":
        report.note = f"预算过紧，最低可行预算约 {report.estimated_total} 元"
    _log(wrap_msg("budget_report", "budget", "orchestrator", report))

    state = PlanState(
        request=request,
        subtasks=tasks,
        results=results,
        weather=weather,
        itinerary=itinerary,
        report=report,
        planner=planner,
    )
    yield PlanEvent("done", state)


async def plan(user_input: str, llm: LLMClient, io=None) -> PlanState:
    """非流式入口（测试 / 评估脚本用）：消费 stream_plan 返回最终状态。"""
    state: PlanState | None = None
    async for event in stream_plan(user_input, llm, io):
        if event.stage == "done":
            state = event.payload
    assert state is not None
    return state


def wrap_msg(msg_type: str, sender: str, receiver: str, payload_model) -> Envelope:
    return Envelope(
        msg_type=msg_type, sender=sender, receiver=receiver, payload=payload_model.model_dump()
    )
