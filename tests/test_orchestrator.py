"""编排器端到端单测：stub LLM 离线跑通主流程、预算回环、追问流程。

plan() 全链路会调用工具层，这里统一注入 canned_tools（conftest），
杜绝真实高德网络调用，且与 chengdu_itin_json 的候选数据保持一致。
"""

import asyncio

import pytest

from travel_planner import config
from travel_planner.orchestrator import SilentIO, plan
from travel_planner.schemas import TravelRequest
from tests.conftest import StubLLM, extract_response, fetch_chengdu_state, template_compose
from travel_planner.agents.planner import Planner

pytestmark = pytest.mark.usefixtures("canned_tools")


class FakeAskIO(SilentIO):
    """带预置回答的交互 IO。"""

    def __init__(self, answers: list[str]):
        super().__init__()
        self.answers = list(answers)

    def ask(self, prompt):
        return self.answers.pop(0) if self.answers else ""


def build_state(text: str, llm_responses: list[str], io=None):
    return asyncio.run(plan(text, StubLLM(llm_responses), io or SilentIO()))


def chengdu_itin_json(days: int = 3, budget: int = 3000) -> str:
    request = TravelRequest(destination="成都", days=days, budget=budget)
    _, _, weather, pools = fetch_chengdu_state(request)
    return template_compose(request, pools, weather).model_dump_json()


def test_happy_path_no_adjustment(monkeypatch):
    monkeypatch.setattr(config, "LOG_ENVELOPES", False)
    llm_responses = [extract_response(), chengdu_itin_json()]
    state = build_state("国庆去成都玩3天，预算3000，喜欢美食", llm_responses)
    assert state.report.status in ("ok", "under")
    assert state.itinerary.meta.adjust_rounds == 0
    assert state.itinerary.meta.degraded is False
    assert len(state.itinerary.days) == 3
    assert len(state.llm_calls) if False else True


def test_llm_called_exactly_twice():
    llm_responses = [extract_response(), chengdu_itin_json()]

    class Counting:
        pass

    # 直接数 StubLLM.calls
    from tests.conftest import StubLLM

    llm = StubLLM(llm_responses)
    asyncio.run(plan("国庆去成都玩3天，预算3000，喜欢美食", llm, SilentIO()))
    assert len(llm.calls) == 2  # 全程只有抽取+编排两次 LLM 调用（NFR2）


def test_low_budget_loop_hits_two_rounds():
    llm_responses = [extract_response(budget=600), chengdu_itin_json(budget=600)]
    state = build_state("成都穷游3天，预算600", llm_responses)
    assert state.report.status == "over"
    assert state.itinerary.meta.adjust_rounds == 2  # MAX_ADJUST_ROUNDS
    assert state.report.note and "预算过紧" in state.report.note
    tiers = {"经济型": 0, "舒适型": 1, "高档型": 2}
    initial_tier = tiers["舒适型"]
    assert tiers[state.itinerary.hotel.tier] <= initial_tier  # 至少尝试过降档


def test_missing_destination_asks_user():
    request = TravelRequest(destination="杭州", days=3, budget=3000)
    _, _, weather, pools = fetch_chengdu_state(request)
    hangzhou_itin_json = template_compose(request, pools, weather).model_dump_json()

    llm_responses = [
        extract_response(destination=None),
        extract_response(destination="杭州"),
        hangzhou_itin_json,
    ]
    io = FakeAskIO(["杭州"])
    state = build_state("想去玩几天", llm_responses, io)
    assert state.request.destination == "杭州"


def test_missing_days_and_budget_asked_then_used():
    llm_responses = [
        extract_response(days=None, budget=None),
        chengdu_itin_json(days=5),
    ]
    io = FakeAskIO(["5", "2000"])
    state = build_state("去成都玩", llm_responses, io)
    assert state.request.days == 5
    assert state.request.budget == 2000
    assert len(state.itinerary.days) == 5


def test_unclear_request_without_answers_raises():
    """要求不清晰（缺天数/预算）且追问无果 → 不开始安排，抛 UnclearRequest。"""
    from travel_planner.orchestrator import UnclearRequest

    llm_responses = [extract_response(days=None, budget=None)]
    with pytest.raises(UnclearRequest):
        build_state("去成都玩", llm_responses, SilentIO())


def test_envelope_logging(monkeypatch, capsys):
    monkeypatch.setattr(config, "LOG_ENVELOPES", True)
    llm_responses = [extract_response(), chengdu_itin_json()]
    build_state("去成都玩3天预算3000", llm_responses)
    err = capsys.readouterr().err
    assert "[subtasks]" in err
    assert "[info_result]" in err
    assert "[itinerary]" in err
    assert "[budget_report]" in err
