"""工具层单测：确定性天气、数据查询、故障注入、超时兜底。"""

import asyncio

import pytest

from travel_planner import config
from travel_planner.agents.info import InfoAgent
from travel_planner.agents.planner import Planner
from travel_planner.schemas import SubTask, SubTaskFilters, TravelRequest
from travel_planner.tools.loader import CityNotFoundError, load_city, normalize_city
from travel_planner.tools.weather import get_weather


def test_weather_deterministic():
    a = asyncio.run(get_weather("成都", ["2026-10-01", "2026-10-02"]))
    b = asyncio.run(get_weather("成都", ["2026-10-01", "2026-10-02"]))
    assert a["data"] == b["data"]
    for row in a["data"]:
        assert row["rain"] == (row["condition"] in ("小雨", "大雨"))
        assert row["temp_low"] <= row["temp_high"]


def test_weather_unknown_city_raises():
    with pytest.raises(CityNotFoundError):
        asyncio.run(get_weather("克拉玛依", ["2026-10-01"]))


def test_normalize_city_suffix():
    assert normalize_city("成都市") == "成都"
    book = load_city("成都市")  # 带后缀也能加载
    assert "attractions" in book


def test_attractions_pref_ranking_and_count():
    from travel_planner.tools.poi import get_attractions

    out = asyncio.run(get_attractions("成都", 3, {"preferences": ["美食"], "indoor_only": False}))
    assert len(out["data"]) <= 3
    assert all("美食" in a["tags"] for a in out["data"])  # 命中偏好的排前面
    out_all = asyncio.run(get_attractions("成都", 99, {}))
    # 数据不足 count 条就返回实际条数（文档 6.2）
    assert len(out_all["data"]) == len(load_city("成都")["attractions"])


def test_attractions_indoor_only_strict():
    from travel_planner.tools.poi import get_attractions

    out = asyncio.run(get_attractions("成都", 20, {"preferences": [], "indoor_only": True}))
    assert out["data"] and all(a["indoor"] for a in out["data"])


def test_hotels_cover_tiers_ordered():
    from travel_planner.tools.hotel import get_hotels

    out = asyncio.run(get_hotels("成都", 5))
    tiers = [h["tier"] for h in out["data"]]
    order = {"经济型": 0, "舒适型": 1, "高档型": 2}
    assert [order[t] for t in tiers] == sorted(order[t] for t in tiers)


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


def test_weather_fallback_uses_generic_climate():
    from travel_planner.tools.weather import fallback_climate, make_weather

    climate = fallback_climate()
    row = make_weather("某城", None, 0, climate)
    assert row["city"] == "某城"
    assert row["date"] is None


def test_all_city_data_valid():
    """数据验收（文档 6.1）：pydantic 校验 + 价格/评分区间 + 数量规格。"""
    import json
    from pathlib import Path

    from travel_planner.schemas import (
        ATTRACTION_EXTRA_TAGS,
        HOTEL_TIERS,
        PREFERENCES,
        Attraction,
        Hotel,
        Restaurant,
    )
    from travel_planner import config as cfg

    data_dir = Path(__file__).resolve().parents[1] / "src" / "travel_planner" / "tools" / "data"
    files = sorted((data_dir / "cities").glob("*.json"))
    assert len(files) >= 10, "首批应覆盖 10 个城市"
    allowed_tags = set(PREFERENCES) | set(ATTRACTION_EXTRA_TAGS)
    tier_range = cfg.HOTEL_TIER_PRICE_RANGE
    for f in files:
        book = json.loads(f.read_text(encoding="utf-8"))
        assert 8 <= len(book["attractions"]) <= 12, f.name
        assert 10 <= len(book["restaurants"]) <= 14, f.name
        assert 5 <= len(book["hotels"]) <= 8, f.name
        for a in book["attractions"]:
            m = Attraction.model_validate(a)
            assert set(m.tags) <= allowed_tags, f"{f.name}: {m.tags}"
        for r in book["restaurants"]:
            Restaurant.model_validate(r)
        tiers_seen = set()
        for h in book["hotels"]:
            m = Hotel.model_validate(h)
            lo, hi = tier_range[m.tier]
            assert lo <= m.price_per_night <= hi, f"{f.name}: {h}"
            tiers_seen.add(m.tier)
        assert tiers_seen == set(HOTEL_TIERS), f"{f.name} 缺档位"
