"""美食查询工具（文档 6.2 节）：按评分降序取候选。"""

from __future__ import annotations

from travel_planner.tools.loader import ToolResult, load_city, maybe_fail


async def get_restaurants(city: str, count: int) -> ToolResult:
    maybe_fail("get_restaurants")
    items = load_city(city)["restaurants"]
    ranked = sorted(items, key=lambda r: r["rating"], reverse=True)
    return ToolResult(source="local", data=ranked[:count])
