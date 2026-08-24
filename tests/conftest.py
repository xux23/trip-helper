"""测试公共设施：Stub LLM 与成都数据构造器（纯内存，不碰工具层/网络）。

在线化后工具层依赖高德 API，单测不再走真实工具；这里直接构造 InfoResult
候选数据，供 Planner / 编排器 / 模板编排的离线测试使用。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from travel_planner.agents.planner import CandidatePools, Planner, template_compose
from travel_planner.schemas import InfoResult, TravelRequest, Weather

# 成都风格 canned 数据（在线数据映射后的形状，字段与 schema 一致）

_ATTRACTIONS = [
    {"name": "宽窄巷子", "tags": ["街区", "免费", "美食"], "price": 0, "duration_hours": 2.5, "indoor": False, "rating": 4.6, "area": "青羊区"},
    {"name": "成都博物馆", "tags": ["博物馆", "免费"], "price": 0, "duration_hours": 2.0, "indoor": True, "rating": 4.6, "area": "青羊区"},
    {"name": "武侯祠", "tags": ["人文", "博物馆"], "price": 50, "duration_hours": 2.0, "indoor": True, "rating": 4.6, "area": "武侯区"},
    {"name": "大熊猫基地", "tags": ["亲子", "自然"], "price": 55, "duration_hours": 3.5, "indoor": False, "rating": 4.7, "area": "成华区"},
    {"name": "春熙路", "tags": ["购物", "街区"], "price": 0, "duration_hours": 2.5, "indoor": False, "rating": 4.5, "area": "锦江区"},
    {"name": "人民公园", "tags": ["公园", "免费"], "price": 0, "duration_hours": 2.0, "indoor": False, "rating": 4.4, "area": "青羊区"},
    {"name": "杜甫草堂", "tags": ["人文", "自然"], "price": 50, "duration_hours": 2.5, "indoor": False, "rating": 4.5, "area": "青羊区"},
    {"name": "锦里", "tags": ["街区", "美食", "夜生活"], "price": 0, "duration_hours": 2.0, "indoor": False, "rating": 4.5, "area": "武侯区"},
    {"name": "四川博物院", "tags": ["博物馆", "免费"], "price": 0, "duration_hours": 2.0, "indoor": True, "rating": 4.5, "area": "青羊区"},
    {"name": "东郊记忆", "tags": ["街区", "小众"], "price": 0, "duration_hours": 3.0, "indoor": False, "rating": 4.3, "area": "成华区"},
]

_RESTAURANTS = [
    {"name": "陈麻婆豆腐", "tags": ["美食", "川菜"], "price_per_person": 60, "meals": ["lunch", "dinner"], "rating": 4.6, "area": "青羊区"},
    {"name": "龙抄手", "tags": ["美食", "小吃"], "price_per_person": 40, "meals": ["lunch"], "rating": 4.5, "area": "锦江区"},
    {"name": "蜀九香火锅", "tags": ["美食", "火锅"], "price_per_person": 100, "meals": ["lunch", "dinner"], "rating": 4.7, "area": "武侯区"},
    {"name": "钟水饺", "tags": ["美食", "小吃"], "price_per_person": 35, "meals": ["lunch"], "rating": 4.5, "area": "青羊区"},
    {"name": "皇城老妈火锅", "tags": ["美食", "火锅"], "price_per_person": 120, "meals": ["dinner"], "rating": 4.6, "area": "武侯区"},
    {"name": "夫妻肺片", "tags": ["美食", "川菜"], "price_per_person": 70, "meals": ["lunch", "dinner"], "rating": 4.6, "area": "锦江区"},
    {"name": "玉林串串香", "tags": ["美食", "小吃"], "price_per_person": 45, "meals": ["dinner"], "rating": 4.4, "area": "武侯区"},
    {"name": "苍蝇馆子", "tags": ["美食", "川菜"], "price_per_person": 55, "meals": ["lunch", "dinner"], "rating": 4.3, "area": "青羊区"},
    {"name": "麻辣烫小馆", "tags": ["美食", "小吃"], "price_per_person": 50, "meals": ["lunch", "dinner"], "rating": 4.2, "area": "锦江区"},
    {"name": "老码头冒菜", "tags": ["美食", "川菜"], "price_per_person": 48, "meals": ["lunch", "dinner"], "rating": 4.1, "area": "青羊区"},
]

_HOTELS = [
    {"name": "汉庭酒店", "tier": "经济型", "price_per_night": 160, "area": "锦江区", "tags": []},
    {"name": "如家快捷酒店", "tier": "经济型", "price_per_night": 200, "area": "锦江区", "tags": []},
    {"name": "全季酒店", "tier": "舒适型", "price_per_night": 450, "area": "青羊区", "tags": []},
    {"name": "香格里拉大酒店", "tier": "高档型", "price_per_night": 900, "area": "锦江区", "tags": []},
]


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
    """构造纯内存的候选数据（不调用工具层/高德）。"""
    request = request or make_request()
    results = [
        InfoResult(task_id="T1", type="attractions", source="online", data=list(_ATTRACTIONS)),
        InfoResult(task_id="T2", type="food", source="online", data=list(_RESTAURANTS)),
        InfoResult(task_id="T3", type="hotels", source="online", data=list(_HOTELS)),
    ]
    weather = [
        Weather(city="成都", date=None, condition="晴", temp_low=18, temp_high=28, rain=False)
        for _ in range(request.days)
    ]
    planner = Planner(None)
    pools = planner._collect_pools(results)
    return request, results, weather, pools


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
        "search_keyword": None,
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


@pytest.fixture
def canned_tools(monkeypatch):
    """把 4 个工具换成 canned 数据源，plan() 全链路离线跑（编排/搜索测试用）。

    注意：返回全量候选（不按 count 切片），保证与 chengdu_itin_json 构造
    行程时用的候选池完全一致，避免"不在候选列表"的校验误报。
    """
    from travel_planner.tools import food, hotel, poi, weather

    async def fake_attractions(city, count, filters):
        items = list(_ATTRACTIONS)
        prefs = set((filters or {}).get("preferences") or [])
        if (filters or {}).get("indoor_only"):
            items = [a for a in items if a["indoor"]]
        matched = [a for a in items if prefs & set(a["tags"])]
        rest = [a for a in items if not (prefs & set(a["tags"]))]
        ranked = sorted(matched, key=lambda a: a["rating"], reverse=True) + sorted(
            rest, key=lambda a: a["rating"], reverse=True
        )
        return {"source": "online", "data": ranked}

    async def fake_food(city, count):
        ranked = sorted(_RESTAURANTS, key=lambda r: r["rating"], reverse=True)
        return {"source": "online", "data": ranked}

    async def fake_hotels(city, count):
        order = {"经济型": 0, "舒适型": 1, "高档型": 2}
        ranked = sorted(_HOTELS, key=lambda h: (order[h["tier"]], h["price_per_night"]))
        return {"source": "online", "data": ranked}

    async def fake_weather(city, dates):
        return {
            "source": "online",
            "data": [
                {"city": city, "date": d, "condition": "晴", "temp_low": 18, "temp_high": 28, "rain": False}
                for d in dates
            ],
        }

    monkeypatch.setattr(poi, "get_attractions", fake_attractions)
    monkeypatch.setattr(food, "get_restaurants", fake_food)
    monkeypatch.setattr(hotel, "get_hotels", fake_hotels)
    monkeypatch.setattr(weather, "get_weather", fake_weather)


@pytest.fixture
def chengdu_itin():
    itin, pools, results, weather = template_itinerary()
    return itin, pools
