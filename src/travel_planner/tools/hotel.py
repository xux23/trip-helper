"""酒店查询工具（文档 6.2 节）。

酒店没有评分字段，按 档位（低→高）+ 价格升序返回，保证候选覆盖三档价位，
供编排和预算降档挑选。
"""

from __future__ import annotations

from travel_planner.tools.loader import ToolResult, load_city, maybe_fail

_TIER_ORDER = {"经济型": 0, "舒适型": 1, "高档型": 2}


async def get_hotels(city: str, count: int) -> ToolResult:
    maybe_fail("get_hotels")
    items = load_city(city)["hotels"]
    ranked = sorted(items, key=lambda h: (_TIER_ORDER[h["tier"]], h["price_per_night"]))
    return ToolResult(source="local", data=ranked[:count])
