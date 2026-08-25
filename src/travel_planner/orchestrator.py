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
    web_hits: list | None = None  # 免费网页搜索摘录（搜索模式不确定时的参考）
    web_source: str | None = None


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


class UnclearRequest(Exception):
    """追问后需求仍不明确（缺天数/预算），不开始安排。"""


_MISSING_LABEL = {"days": "天数", "budget": "预算"}


def _missing_names(missing: set[str]) -> str:
    return "、".join(_MISSING_LABEL.get(f, f) for f in sorted(missing))


class SilentIO:
    """非交互 IO：追问返回空、提示静默收集（评估脚本用）。

    模糊需求（缺天数/预算且无回答）会触发 UnclearRequest 中止，不静默用默认值。
    """

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

    yield PlanEvent("stage", "🤔 先看看你想怎么玩…")

    # ① 需求理解：目的地缺失时追问一次，补全后重新抽取（FR1 / U3）
    try:
        outcome: ExtractionOutcome = await planner.extract_request(user_input)
    except MissingDestination as e:
        answer = (ask("没认出你想去的城市，想去哪儿？") or "").strip()
        if not answer:
            raise ExtractError("缺少目的地，无法继续规划") from e
        outcome = await planner.re_extract_with_destination(user_input, answer)

    for notice in outcome.notices:
        _notify(io, notice)
    request = outcome.request
    yield PlanEvent("request", request)

    # ②' 搜索模式：只搜景点列表，不拆任务不编排（输入即搜索，U-搜索 用例）
    if request.search_keyword is not None:
        yield PlanEvent("stage", f"🔎 正在帮你找 {request.destination}「{request.search_keyword}」景点…")
        attrs, source = await info.search(request.destination, request.search_keyword)
        web_hits, web_source = await info.web_info(request.destination, request.search_keyword)
        state = PlanState(
            request=request,
            planner=planner,
            search_results=attrs,
            search_source=source,
            web_hits=web_hits,
            web_source=web_source,
        )
        yield PlanEvent("done", state)
        return

    # ② 缺失字段追问：要求不清晰不开始安排（不静默用默认值），最多问 3 轮
    missing = set(outcome.missing_fields)
    for _ in range(3):
        if "days" in missing:
            raw = _parse_int(ask("打算玩几天？"))
            if raw:
                request.days = min(max(raw, 1), 7)  # 天数钳制在 [1,7]
                missing.discard("days")
                _notify(io, f"好的，按 {request.days} 天安排")
        if "budget" in missing:
            raw = _parse_int(ask("人均预算大概多少（元）？"))
            if raw and raw > 0:
                request.budget = raw
                missing.discard("budget")
                _notify(io, f"好的，预算按 {request.budget} 元")
        if not missing:
            break
        _notify(io, f"还差：{_missing_names(missing)}，说清楚才能开始安排")
    if missing:
        raise UnclearRequest(
            f"需求不明确：缺少{_missing_names(missing)}，无法安排行程。"
            "请说明天数与预算，例如「去成都玩3天，预算2000」"
        )

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

    yield PlanEvent("stage", f"📋 正在查询 {request.destination} 的景点、美食和酒店…")

    # ④ 信息获取：超时→重试→兜底
    results, weather = await info.run(tasks, request)
    for r in results:
        _log(wrap_msg("info_result", "info", "planner", r))
    yield PlanEvent("info", results)

    yield PlanEvent("stage", "🗺 正在安排每天的行程…")

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
        # 回环后仍超标 = 最低可行配置都超预算，CLI 不输出行程，改为提示"预算不现实"
        report.note = f"预算过紧：按最低配置（经济酒店+免费景点+压缩行程）估算仍约需 {report.estimated_total} 元/人"
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
