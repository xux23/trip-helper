"""在线景点搜索工具（搜索模式，文档第 6 章）。

关键词含"免费"时：按风景名胜类拉取后过滤 price==0（免费倾向），
因为高德 keywords 不支持按"免费"检索。
"""

from __future__ import annotations

from travel_planner.tools import amap
from travel_planner.tools.loader import ToolResult, maybe_fail


async def search_pois(city: str, keyword: str, count: int = 25) -> ToolResult:
    maybe_fail("search_pois")
    client = amap.make_amap_client()
    free_only = "免费" in (keyword or "")
    kw = (keyword or "").replace("免费", "").strip() or None
    # keywords 与 types 二选一：有关键词就不带 types，否则按风景名胜类搜索
    pois = await client.search_pois(
        city, keywords=kw, types=None if kw else "风景名胜", offset=count
    )
    items = [amap.map_attraction(p) for p in pois]
    if free_only:
        items = [a for a in items if a["price"] == 0]
    return ToolResult(source="online", data=items[:count])
