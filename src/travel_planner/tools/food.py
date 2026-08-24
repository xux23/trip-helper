"""美食查询工具（文档 6.2 节）：在线高德"餐饮服务"搜索，按评分降序取候选。

高德会把带餐饮的宾馆/酒店也标成"餐饮服务"，用 amap.is_hotelish 过滤掉，
避免行程里出现"酒店当餐厅"。
"""

from __future__ import annotations

from travel_planner.tools import amap
from travel_planner.tools.loader import ToolResult, maybe_fail


async def get_restaurants(city: str, count: int) -> ToolResult:
    maybe_fail("get_restaurants")
    client = amap.make_amap_client()
    pois = await client.search_pois(city, types="餐饮服务", offset=25)
    items = [amap.map_restaurant(p) for p in pois if not amap.is_hotelish(p)]
    ranked = sorted(items, key=lambda r: r["rating"], reverse=True)
    return ToolResult(source="online", data=ranked[:count])
