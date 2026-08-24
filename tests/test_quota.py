"""免费配额控制单测：计数/持久化/跨日重置/超限上抛（不兜底）。"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from travel_planner.agents.info import InfoAgent
from travel_planner.schemas import SubTask, SubTaskFilters
from travel_planner.tools import poi
from travel_planner.tools.amap import QuotaExhaustedError, QuotaTracker


def test_quota_count_persist_and_exhaust(tmp_path):
    f = tmp_path / "quota.json"
    q = QuotaTracker(f, {"search": 2}, today=date(2026, 8, 24))
    q.check("search")
    q.record("search")
    q.check("search")
    q.record("search")
    with pytest.raises(QuotaExhaustedError):
        q.check("search")
    # 新进程重新加载：计数持久化仍然生效
    q2 = QuotaTracker(f, {"search": 2}, today=date(2026, 8, 24))
    with pytest.raises(QuotaExhaustedError):
        q2.check("search")


def test_quota_resets_next_day(tmp_path):
    f = tmp_path / "quota.json"
    QuotaTracker(f, {"search": 2}, today=date(2026, 8, 24)).record("search")
    q = QuotaTracker(f, {"search": 2}, today=date(2026, 8, 25))
    q.check("search")  # 新的一天不抛
    assert q.count("search") == 0


def test_quota_exhausted_services(tmp_path):
    q = QuotaTracker(tmp_path / "q.json", {"search": 1, "weather": 5}, today=date(2026, 8, 24))
    q.record("search")
    assert q.exhausted_services() == ["search"]


def test_quota_exhausted_propagates_through_info(monkeypatch):
    """配额耗尽不能被 InfoAgent 兜底，必须上抛。"""

    async def boom(city, count, filters=None):
        raise QuotaExhaustedError("配额用完")

    monkeypatch.setattr(poi, "get_attractions", boom)
    sub = SubTask(task_id="T1", type="attractions", destination="成都", count=3,
                  filters=SubTaskFilters())
    with pytest.raises(QuotaExhaustedError):
        asyncio.run(InfoAgent()._run_task(sub))
