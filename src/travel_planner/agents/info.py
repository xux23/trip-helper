"""Info Agent（文档 4.2 节）：调用工具层获取结构化数据，负责超时、重试与兜底。

每次调用：超时 3 秒 → 失败重试 1 次 → 仍失败用通用兜底数据（source=fallback）。
唯一例外：高德免费配额耗尽（QuotaExhaustedError）不走兜底，直接上抛——
由 CLI 层停止整个程序（用户约定）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from travel_planner import config
from travel_planner.schemas import Attraction, InfoResult, SubTask, SubTaskFilters, TravelRequest, Weather
from travel_planner.tools import food, hotel, poi, search as _search_tool
from travel_planner.tools.amap import QuotaExhaustedError
from travel_planner.tools.loader import load_fallback
from travel_planner.tools.weather import fallback_climate, get_weather, make_weather

_TOOL_BY_TYPE = {
    "attractions": lambda t: poi.get_attractions(t.destination, t.count, t.filters.model_dump()),
    "food": lambda t: food.get_restaurants(t.destination, t.count),
    "hotels": lambda t: hotel.get_hotels(t.destination, t.count),
}
_FALLBACK_CATEGORY = {"attractions": "attractions", "food": "restaurants", "hotels": "hotels"}


def build_dates(request_date: str | None, days: int) -> list[str | None]:
    """日期未定时返回 [None, ...]，天气按当月典型值生成。"""
    if not request_date:
        return [None] * days
    start = datetime.strptime(request_date, "%Y-%m-%d").date()
    return [(start + timedelta(days=i)).isoformat() for i in range(days)]


async def _call_with_retry(call, *args):
    """超时→重试1次→抛出最后一次异常，由上层走兜底。配额耗尽直接上抛不重试。"""
    last: Exception | None = None
    for _ in range(config.TOOL_RETRY_TIMES + 1):
        try:
            return await asyncio.wait_for(call(*args), timeout=config.TOOL_TIMEOUT_SECONDS)
        except QuotaExhaustedError:
            raise
        except Exception as e:  # 超时/AmapError/OSError 一律可兜底
            last = e
    assert last is not None
    raise last


class InfoAgent:
    async def run(
        self, tasks: list[SubTask], request: TravelRequest
    ) -> tuple[list[InfoResult], list[Weather]]:
        """顺序执行子任务；返回结果列表与逐日天气。并发留作后续优化（文档 2.3）。"""
        results = [await self._run_task(t) for t in tasks]
        return results, await self._fetch_weather(request)

    async def search(self, city: str, keyword: str) -> tuple[list[Attraction], str]:
        """搜索模式：在线搜景点，返回 (attractions, source)。失败走通用兜底。"""
        try:
            raw = await _call_with_retry(_search_tool.search_pois, city, keyword)
            return [Attraction.model_validate(d) for d in raw["data"]], raw["source"]
        except QuotaExhaustedError:
            raise
        except Exception as e:
            items = self._fallback_result(
                SubTask(
                    task_id="S1", type="attractions", destination=city, count=25,
                    filters=SubTaskFilters(),
                ),
                e,
            )
            return items.attractions(), "fallback"

    async def _run_task(self, task: SubTask) -> InfoResult:
        try:
            raw = await _call_with_retry(_TOOL_BY_TYPE[task.type], task)
            return InfoResult(
                task_id=task.task_id,
                type=task.type,
                source=raw["source"],
                data=list(raw["data"]),
                error=None,
            )
        except QuotaExhaustedError:
            raise
        except Exception as e:
            return self._fallback_result(task, e)

    def _fallback_result(self, task: SubTask, error: Exception) -> InfoResult:
        book = load_fallback()
        items = list(book[_FALLBACK_CATEGORY[task.type]])
        prefs = set(task.filters.preferences or [])
        if task.filters.indoor_only:
            items = [x for x in items if x.get("indoor")]
        if prefs and task.type == "attractions":
            matched = [x for x in items if prefs & set(x["tags"])]
            rest = [x for x in items if not (prefs & set(x["tags"]))]
            items = matched + rest
        items.sort(key=lambda x: -float(x.get("rating", 0)))
        return InfoResult(
            task_id=task.task_id,
            type=task.type,
            source="fallback",
            data=items[: task.count],
            error=str(error) or error.__class__.__name__,
        )

    async def _fetch_weather(self, request: TravelRequest) -> list[Weather]:
        dates = build_dates(request.date, request.days)
        try:
            raw = await _call_with_retry(get_weather, request.destination, dates)
            return [Weather.model_validate(d) for d in raw["data"]]
        except QuotaExhaustedError:
            raise
        except Exception:
            climate = fallback_climate()
            return [
                Weather.model_validate(make_weather(request.destination, d, i, climate))
                for i, d in enumerate(dates)
            ]
