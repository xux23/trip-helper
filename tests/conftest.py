"""测试公共设施：Stub LLM 与成都数据构造器。"""

from __future__ import annotations

import asyncio
import json

import pytest

from travel_planner.agents.info import InfoAgent
from travel_planner.agents.planner import CandidatePools, Planner, template_compose
from travel_planner.schemas import TravelRequest


class StubLLM:
    """按顺序返回预置响应的假客户端；记录调用供断言。"""

    model = "stub"

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def chat(self, messages, *, temperature: float, max_tokens: int):
        self.calls.append(
            {"system": messages[0]["content"], "user": messages[-1]["content"],
             "temperature": temperature, "max_tokens": max_tokens}
        )
        if not self.responses:
            raise AssertionError("StubLLM 响应已耗尽")

        class R:
            text = self.responses.pop(0)
            prompt_tokens = 100
            completion_tokens = 50

        return R()


def run(coro):
    return asyncio.run(coro)


def make_request(**kw) -> TravelRequest:
    base = dict(destination="成都", days=3, budget=3000)
    base.update(kw)
    return TravelRequest(**base)


def fetch_chengdu_state(request: TravelRequest | None = None):
    """走真实工具层拿候选与天气（离线、确定性）。"""
    request = request or make_request()
    planner = Planner(None)
    tasks = planner.make_subtasks(request)
    results, weather = run(InfoAgent().run(tasks, request))
    return request, results, weather, planner._collect_pools(results)


def template_itinerary(request: TravelRequest | None = None):
    request, results, weather, pools = fetch_chengdu_state(request)
    itin = template_compose(request, pools, weather)
    return itin, pools, results, weather


def extract_response(**overrides) -> str:
    payload = {
        "destination": "成都",
        "days": 3,
        "budget": 3000,
        "departure_city": None,
        "date": None,
        "preferences": [],
        "party_size": 1,
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


@pytest.fixture
def chengdu_itin():
    itin, pools, results, weather = template_itinerary()
    return itin, pools
