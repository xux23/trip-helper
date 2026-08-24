"""确定性模拟天气（文档 5.3 / 6.2 节）。

生成规则：seed = sha256(city + date)，按"城市-月份气候表"的权重选 condition，
温度在气候表区间内取整。同一 city+date 永远得到相同结果。
不用内置 hash()——它跨进程随机化，无法满足可复现要求。
"""

from __future__ import annotations

import hashlib
import random
from typing import Any

from travel_planner.tools.loader import ToolResult, load_city, maybe_fail


def _normalize_climate(entry: Any) -> dict[str, Any]:
    """兼容两种存储格式：{conditions,temp_low,temp_high} 或 [conditions, low, high]。"""
    if isinstance(entry, list) and len(entry) == 3:
        return {"conditions": entry[0], "temp_low": entry[1], "temp_high": entry[2]}
    return entry


def climate_entry(city: str, date: str | None) -> dict[str, Any]:
    """返回某城市某月（或当月/默认）的气候条目。日期未定时按当月典型值。

    城市未知时抛 CityNotFoundError，由 Info Agent 决定改用 fallback 气候表。
    """
    data = load_city(city)
    month = (date or "")[5:7].lstrip("0") or None
    entry = data["climate"].get(month) if month else None
    if entry is None:
        entry = data["climate"]["default"]
    return _normalize_climate(entry)


def fallback_climate() -> dict[str, Any]:
    from travel_planner.tools.loader import load_fallback

    return _normalize_climate(load_fallback()["climate"]["default"])


def make_weather(city: str, date: str | None, index: int, climate: dict[str, Any]) -> dict[str, Any]:
    """按气候表确定性生成一天的天气。"""
    seed_key = f"{city}|{date or f'unknown-{index}'}"
    rng = random.Random(int(hashlib.sha256(seed_key.encode()).hexdigest(), 16))
    conditions: dict[str, float] = climate["conditions"]
    names = list(conditions)
    weights = [conditions[n] for n in names]
    condition = rng.choices(names, weights=weights, k=1)[0]
    temp_low = int(climate["temp_low"])
    temp_high = int(climate["temp_high"])
    low = rng.randint(temp_low, temp_high - 1) if temp_high > temp_low else temp_low
    high = rng.randint(low + 1, temp_high) if temp_high > low else temp_high
    return {
        "city": city,
        "date": date,
        "condition": condition,
        "temp_low": low,
        "temp_high": high,
        "rain": condition in ("小雨", "大雨"),
    }


async def get_weather(city: str, dates: list[str | None]) -> ToolResult:
    """按 destination + 日期逐天生成天气。dates 元素为 YYYY-MM-DD 或 None。"""
    maybe_fail("get_weather")
    out = [make_weather(city, d, i, climate_entry(city, d)) for i, d in enumerate(dates)]
    return ToolResult(source="local", data=out)
