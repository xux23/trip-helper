"""酒店查询工具（文档 6.2 节）：在线高德"住宿服务"搜索。

高德 POI 无房价字段，档位由品牌/类型词表启发式判定，价格取配置档位区间中点
（估算值，tag 标注"价格估计"）。按 档位（低→高）+ 价格升序返回。
"""

from __future__ import annotations

from travel_planner.tools import amap
from travel_planner.tools.loader import ToolResult, maybe_fail

_TIER_ORDER = {"经济型": 0, "舒适型": 1, "高档型": 2}


async def get_hotels(city: str, count: int) -> ToolResult:
    maybe_fail("get_hotels")
    client = amap.make_amap_client()
    pois = await client.search_pois(city, types="住宿服务", offset=25)
    items = [amap.map_hotel(p) for p in pois]
    ranked = sorted(items, key=lambda h: (_TIER_ORDER[h["tier"]], h["price_per_night"]))
    return ToolResult(source="online", data=ranked[:count])
