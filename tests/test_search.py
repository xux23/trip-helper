"""搜索模式单测：意图抽取、编排分支、免费过滤。"""

from __future__ import annotations

import asyncio

from travel_planner.agents.planner import Planner
from travel_planner.orchestrator import SilentIO, plan
from travel_planner.schemas import Attraction
from tests.conftest import StubLLM, extract_response, run


async def _no_web(self, city, keyword):  # 桩：免费网页搜索在单测里不碰网络
    return [], "fallback"


def test_extract_search_keyword():
    llm = StubLLM([extract_response(search_keyword="免费", days=None, budget=None)])
    outcome = run(Planner(llm).extract_request("找成都免费景点"))
    assert outcome.request.search_keyword == "免费"
    assert outcome.request.destination == "成都"
    # 搜索模式下 days/budget 缺失也被标记，但编排分支不会追问（见 orchestrator）


def test_plan_search_mode_skips_days_budget_questions(monkeypatch):
    """搜索模式不追问天数/预算：即便缺失也直接进入搜索。"""
    canned = Attraction(
        name="人民公园", tags=["公园", "免费"], price=0, duration_hours=2.0,
        indoor=False, rating=4.5, area="青羊区",
    )

    async def fake_search(self, city, keyword):  # 实例方法：带 self
        return [canned], "online"

    class NoAskIO(SilentIO):
        def __init__(self):
            super().__init__()
            self.asked = 0

        def ask(self, prompt):
            self.asked += 1
            return ""

    monkeypatch.setattr("travel_planner.agents.info.InfoAgent.search", fake_search)
    monkeypatch.setattr("travel_planner.agents.info.InfoAgent.web_info", _no_web)
    io = NoAskIO()
    llm = StubLLM([extract_response(search_keyword="免费", days=None, budget=None)])
    state = asyncio.run(plan("找成都免费景点", llm, io))
    assert state.search_results == [canned]
    assert io.asked == 0  # 天数/预算缺失也没有追问


def test_plan_search_mode_returns_results(monkeypatch):
    canned = Attraction(
        name="人民公园", tags=["公园", "免费"], price=0, duration_hours=2.0,
        indoor=False, rating=4.5, area="青羊区",
    )

    async def fake_search(self, city, keyword):  # 实例方法：带 self
        return [canned], "online"

    monkeypatch.setattr("travel_planner.agents.info.InfoAgent.search", fake_search)
    monkeypatch.setattr("travel_planner.agents.info.InfoAgent.web_info", _no_web)
    llm = StubLLM([extract_response(search_keyword="免费", days=None, budget=None)])
    state = asyncio.run(plan("找成都免费景点", llm, SilentIO()))
    assert state.search_results == [canned]
    assert state.search_source == "online"
    assert state.itinerary is None  # 搜索模式无行程
    assert len(llm.calls) == 1  # 只调了一次抽取，不编排（NFR2 成本口径）


def test_search_fallback_source_on_failure(monkeypatch):
    """在线搜索工具失败 → InfoAgent 走通用兜底，流程不中断。"""
    from travel_planner.tools import search as search_tool

    async def broken(*a, **k):
        raise OSError("网络不可用")

    monkeypatch.setattr(search_tool, "search_pois", broken)
    monkeypatch.setattr("travel_planner.agents.info.InfoAgent.web_info", _no_web)
    llm = StubLLM([extract_response(search_keyword="免费", days=None, budget=None)])
    state = asyncio.run(plan("找成都免费景点", llm, SilentIO()))
    assert state.search_source == "fallback"
    assert state.search_results  # 兜底也有数据
