"""在线天气（文档 5.3 / 6.2 节）：高德 4 天预报，超出/失败用通用气候兜底。

流程：城市名 → geocode 拿 adcode → weatherInfo 拿 casts（约 4 天）。
请求日期超出预报范围时复用最后一天的预报；天气文本按词表映射到
schema 的 5 种 condition。兜底气候沿用确定性生成（sha256 种子），保证可复现。
"""

from __future__ import annotations

from typing import Any

from travel_planner.tools import amap
from travel_planner.tools.loader import ToolResult, load_fallback, maybe_fail


def fallback_climate() -> dict[str, Any]:
    return _normalize_climate(load_fallback()["climate"]["default"])


def _normalize_climate(entry: Any) -> dict[str, Any]:
    """兼容两种存储格式：{conditions,temp_low,temp_high} 或 [conditions, low, high]。"""
    if isinstance(entry, list) and len(entry) == 3:
        return {"conditions": entry[0], "temp_low": entry[1], "temp_high": entry[2]}
    return entry


def make_weather(city: str, date: str | None, index: int, climate: dict[str, Any]) -> dict[str, Any]:
    """按气候表确定性生成一天的天气（兜底路径用，可复现）。"""
    import hashlib
    import random

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
    """在线逐日天气。dates 元素为 YYYY-MM-DD 或 None。失败由 Info Agent 兜底。"""
    maybe_fail("get_weather")
    client = amap.make_amap_client()
    adcode = await client.geocode(city)
    casts = await client.forecast(adcode)
    if not casts:
        raise amap.AmapError("天气接口无预报数据")
    by_date = {str(c.get("date")): c for c in casts if c.get("date")}
    first = casts[0]
    out = []
    for d in dates:
        cast = by_date.get(d) if d else None
        out.append(amap.make_weather_from_cast(city, d, cast or first))
    return ToolResult(source="online", data=out)
