"""景点查询工具（文档 6.2 节）。

偏好过滤采用"命中优先排序 + 不足补齐"，而非硬过滤：硬过滤可能把候选裁得
不够编排用（U1 要求偏好体现为权重更高，不是只看偏好的景点）。
"""

from __future__ import annotations

from typing import Any

from travel_planner.tools.loader import ToolResult, load_city, maybe_fail


async def get_attractions(city: str, count: int, filters: dict[str, Any]) -> ToolResult:
    maybe_fail("get_attractions")
    items = list(load_city(city)["attractions"])
    prefs = set(filters.get("preferences") or [])
    if filters.get("indoor_only"):
        items = [a for a in items if a["indoor"]]
    matched = [a for a in items if prefs & set(a["tags"])]
    rest = [a for a in items if not (prefs & set(a["tags"]))]
    ranked = sorted(matched, key=lambda a: a["rating"], reverse=True) + sorted(
        rest, key=lambda a: a["rating"], reverse=True
    )
    return ToolResult(source="local", data=ranked[:count])
