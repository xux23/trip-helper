"""工具层单测：高德 POI 映射、在线搜索、故障兜底、天气映射。

在线化后不依赖真实网络：用 FakeAmap 注入预置响应，映射函数直接吃
探测抓取的真实响应形状（biz_ext.rating / cost 字符串等）。
"""

from __future__ import annotations

import asyncio

import pytest

from travel_planner import config
from travel_planner.agents.info import InfoAgent
from travel_planner.agents.planner import Planner
from travel_planner.schemas import SubTask, SubTaskFilters, TravelRequest
from travel_planner.tools import amap
from travel_planner.tools.search import search_pois
from travel_planner.tools.weather import fallback_climate, get_weather, make_weather


class FakeAmap:
    """离线假客户端：注入预置 POI / casts / adcode，记录调用。"""

    def __init__(self, pois=(), casts=(), adcode: str = "510100"):
        self.pois = list(pois)
        self.casts = list(casts)
        self.adcode = adcode
        self.calls: list[tuple] = []

    async def search_pois(self, city, *, keywords=None, types=None, offset=25):
        self.calls.append(("search", city, keywords, types))
        return list(self.pois)

    async def geocode(self, city):
        self.calls.append(("geocode", city))
        return self.adcode

    async def forecast(self, adcode):
        self.calls.append(("forecast", adcode))
        return list(self.casts)


def poi_fixture(
    name: str,
    keytag: str,
    typecode: str,
    type_: str = "风景名胜;风景名胜;风景名胜",
    adname: str = "青羊区",
    rating: str = "4.5",
    biz_extra: dict | None = None,
) -> dict:
    biz: dict = {"rating": rating, "cost": []}
    if biz_extra:
        biz.update(biz_extra)
    return {
        "name": name,
        "keytag": keytag,
        "typecode": typecode,
        "type": type_,
        "adname": adname,
        "biz_ext": biz,
    }


def use_fake(monkeypatch, fake: FakeAmap) -> None:
    monkeypatch.setattr(amap, "make_amap_client", lambda: fake)


# ---- POI → schema 映射 ----

def test_map_attraction_free_park():
    p = poi_fixture("人民公园", "公园", "110200", type_="风景名胜;公园广场;公园")
    a = amap.map_attraction(p)
    assert a["price"] == 0
    assert "免费" in a["tags"]
    assert "价格估算" not in a["tags"]
    assert a["indoor"] is False
    assert a["duration_hours"] == 2.5  # 公园词表
    assert a["area"] == "青羊区"  # adname
    assert a["rating"] == 4.5


def test_map_attraction_paid_zoo():
    p = poi_fixture("成都动物园", "动物园", "110102", type_="风景名胜;公园广场;动物园", rating="4.7")
    a = amap.map_attraction(p)
    assert a["price"] == 30  # 付费词表
    assert "免费" not in a["tags"]
    assert "价格估算" in a["tags"]
    assert a["duration_hours"] == 3.0


def test_map_attraction_museum_indoor():
    p = poi_fixture("成都博物馆", "博物馆", "140100", type_="科教文化服务;博物馆;博物馆")
    a = amap.map_attraction(p)
    assert a["indoor"] is True
    assert a["price"] == 0
    assert "博物馆" in a["tags"]
    assert "人文" in a["tags"]


def test_map_restaurant_uses_cost():
    p = poi_fixture(
        "翠孃孃老火锅", "四川火锅", "050117",
        type_="餐饮服务;中餐厅;火锅店", biz_extra={"cost": "102.00"},
    )
    r = amap.map_restaurant(p)
    assert r["price_per_person"] == 102
    assert r["rating"] == 4.5
    assert "美食" in r["tags"]
    assert r["meals"] == ["lunch", "dinner"]


def test_map_restaurant_default_cost_when_missing():
    p = poi_fixture("无名面馆", "面馆", "050119", type_="餐饮服务;中餐厅;面馆")
    r = amap.map_restaurant(p)
    assert r["price_per_person"] == 60  # 缺失人均默认 60


def test_restaurants_filter_out_hotelish(monkeypatch):
    """高德会把带餐饮的宾馆标成"餐饮服务"，必须从餐厅池过滤。"""
    from travel_planner.tools import food

    hotel_poi = poi_fixture(
        "成都西藏饭店", "宾馆酒店", "050300", type_="餐饮服务;其他餐饮服务;其他", biz_extra={"cost": "120.00"}
    )
    real_poi = poi_fixture("陈麻婆豆腐", "川菜", "050111", biz_extra={"cost": "60.00"})
    assert amap.is_hotelish(hotel_poi) is True
    assert amap.is_hotelish(real_poi) is False
    use_fake(monkeypatch, FakeAmap(pois=[hotel_poi, real_poi]))
    out = asyncio.run(food.get_restaurants("成都", 5))
    assert [r["name"] for r in out["data"]] == ["陈麻婆豆腐"]


def test_map_hotel_tier_heuristic():
    assert amap.map_hotel(poi_fixture("如家快捷酒店", "快捷酒店", "100100"))["tier"] == "经济型"
    assert amap.map_hotel(poi_fixture("全季酒店", "全季酒店", "100100"))["tier"] == "舒适型"
    assert amap.map_hotel(poi_fixture("香格里拉大酒店", "豪华型", "100100"))["tier"] == "高档型"


def test_map_hotel_price_is_tier_midpoint():
    h = amap.map_hotel(poi_fixture("全季酒店", "全季酒店", "100100"))
    lo, hi = config.HOTEL_TIER_PRICE_RANGE["舒适型"]
    assert h["price_per_night"] == (lo + hi) // 2
    assert "价格估计" in h["tags"]


# ---- 天气 ----

def test_weather_condition_mapping():
    assert amap.map_weather_condition("晴") == "晴"
    assert amap.map_weather_condition("多云转晴") == "多云"
    assert amap.map_weather_condition("阴") == "阴"
    assert amap.map_weather_condition("阵雨") == "小雨"
    assert amap.map_weather_condition("暴雨") == "大雨"


def test_make_weather_from_cast():
    cast = {
        "date": "2026-10-01", "dayweather": "多云", "nightweather": "晴",
        "daytemp": "35", "nighttemp": "26",
    }
    w = amap.make_weather_from_cast("成都", "2026-10-01", cast)
    assert w["condition"] == "多云"
    assert w["rain"] is False
    assert (w["temp_low"], w["temp_high"]) == (26, 35)


def test_weather_online_uses_cast_and_adcode(monkeypatch):
    casts = [{"date": "2026-10-01", "dayweather": "晴", "nightweather": "晴",
              "daytemp": "35", "nighttemp": "26"}]
    fake = FakeAmap(casts=casts)
    use_fake(monkeypatch, fake)
    out = asyncio.run(get_weather("成都", ["2026-10-01", "2026-10-02"]))
    assert out["source"] == "online"
    assert len(out["data"]) == 2
    assert out["data"][0]["condition"] == "晴"
    assert out["data"][1]["condition"] == "晴"  # 超出预报范围复用最后一条
    assert ("geocode", "成都") in fake.calls
    assert ("forecast", "510100") in fake.calls


def test_weather_fallback_uses_generic_climate():
    climate = fallback_climate()
    row = make_weather("某城", None, 0, climate)
    assert row["city"] == "某城"
    assert row["date"] is None


# ---- 搜索工具 ----

def test_search_free_only_filters_paid(monkeypatch):
    free = poi_fixture("人民公园", "公园", "110200")
    paid = poi_fixture("成都动物园", "动物园", "110102")
    fake = FakeAmap(pois=[free, paid])
    use_fake(monkeypatch, fake)
    out = asyncio.run(search_pois("成都", "免费"))
    assert out["source"] == "online"
    assert [d["name"] for d in out["data"]] == ["人民公园"]  # 付费的被过滤
    assert fake.calls[0] == ("search", "成都", None, "风景名胜")


def test_search_passes_keyword_without_free_filter(monkeypatch):
    fake = FakeAmap(pois=[poi_fixture("四川博物院", "博物馆", "140100")])
    use_fake(monkeypatch, fake)
    out = asyncio.run(search_pois("成都", "博物馆"))
    assert fake.calls[0] == ("search", "成都", "博物馆", None)
    assert len(out["data"]) == 1


# ---- 景点/餐厅/酒店工具 ----

def test_attractions_pref_ranking_and_count(monkeypatch):
    from travel_planner.tools import poi

    pois = [
        poi_fixture("锦里", "美食街区", "110200", type_="风景名胜;公园广场;广场", adname="武侯区"),
        poi_fixture("宽窄巷子", "美食街区", "110200", type_="风景名胜;公园广场;广场", adname="青羊区"),
        poi_fixture("春熙路", "美食街区", "110200", type_="风景名胜;公园广场;广场", adname="锦江区"),
        poi_fixture("人民公园", "公园", "110200"),
    ]
    use_fake(monkeypatch, FakeAmap(pois=pois))
    out = asyncio.run(poi.get_attractions("成都", 3, {"preferences": ["美食"], "indoor_only": False}))
    assert out["source"] == "online"
    assert len(out["data"]) <= 3
    assert all("美食" in a["tags"] for a in out["data"])  # 命中偏好的排前面


def test_attractions_indoor_only_strict(monkeypatch):
    from travel_planner.tools import poi

    pois = [poi_fixture("成都博物馆", "博物馆", "140100"), poi_fixture("人民公园", "公园", "110200")]
    use_fake(monkeypatch, FakeAmap(pois=pois))
    out = asyncio.run(poi.get_attractions("成都", 20, {"preferences": [], "indoor_only": True}))
    assert out["data"] and all(a["indoor"] for a in out["data"])


def test_restaurants_ranked_by_rating(monkeypatch):
    from travel_planner.tools import food

    pois = [
        poi_fixture("甲餐厅", "川菜", "050111", rating="4.0"),
        poi_fixture("乙餐厅", "川菜", "050111", rating="4.8"),
    ]
    use_fake(monkeypatch, FakeAmap(pois=pois))
    out = asyncio.run(food.get_restaurants("成都", 5))
    assert out["source"] == "online"
    ratings = [r["rating"] for r in out["data"]]
    assert ratings == sorted(ratings, reverse=True)


def test_hotels_cover_tiers_ordered(monkeypatch):
    from travel_planner.tools import hotel

    pois = [
        poi_fixture("香格里拉大酒店", "豪华型", "100100"),
        poi_fixture("如家快捷酒店", "快捷酒店", "100100"),
        poi_fixture("全季酒店", "全季酒店", "100100"),
    ]
    use_fake(monkeypatch, FakeAmap(pois=pois))
    out = asyncio.run(hotel.get_hotels("成都", 5))
    tiers = [h["tier"] for h in out["data"]]
    order = {"经济型": 0, "舒适型": 1, "高档型": 2}
    assert [order[t] for t in tiers] == sorted(order[t] for t in tiers)


# ---- 兜底 ----

def test_info_fallback_on_simulated_failure(monkeypatch):
    """SIMULATE_TOOL_FAILURE=1 且随机数恒为 0 → 全部走兜底，流程不中断。"""
    import random as _random

    monkeypatch.setattr(config, "SIMULATE_TOOL_FAILURE", True)
    monkeypatch.setattr(_random, "random", lambda: 0.0)

    request = TravelRequest(destination="成都", days=2, budget=2000)
    tasks = Planner(None).make_subtasks(request)
    results, weather = asyncio.run(InfoAgent().run(tasks, request))
    assert all(r.source == "fallback" for r in results)
    assert all("故障注入" in (r.error or "") for r in results)
    assert results[0].data  # 兜底也有数据


def test_info_timeout_leads_to_fallback(monkeypatch):
    from travel_planner.tools import poi

    async def slow(*a, **k):
        await asyncio.sleep(0.5)

    monkeypatch.setattr(config, "TOOL_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(poi, "get_attractions", slow)
    sub = SubTask(task_id="T1", type="attractions", destination="成都", count=6,
                  filters=SubTaskFilters())
    result = asyncio.run(InfoAgent()._run_task(sub))
    assert result.source == "fallback"
    assert result.error and "timeout" in result.error.lower()
