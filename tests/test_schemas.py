"""schemas 单测：默认值、钳制、硬约束校验。"""

import pytest
from pydantic import ValidationError

from travel_planner.schemas import (
    Envelope,
    EveningSlot,
    ExtractedRequest,
    Itinerary,
    TravelRequest,
    build_travel_request,
)
from tests.conftest import chengdu_itin  # noqa: F401  (fixture)


def test_build_travel_request_defaults():
    req, notices = build_travel_request(
        ExtractedRequest.model_validate({"destination": "成都"})
    )
    assert req.destination == "成都"
    assert req.days == 3
    assert req.budget == 3000
    assert req.party_size == 1
    assert notices == []


def test_days_clamped_with_notice():
    raw = ExtractedRequest.model_validate({"destination": "成都", "days": 10})
    req, notices = build_travel_request(raw)
    assert req.days == 7
    assert notices and "7" in notices[0]


def test_invalid_preferences_dropped():
    raw = ExtractedRequest.model_validate(
        {"destination": "成都", "preferences": ["美食", "蹦迪"]}
    )
    req, notices = build_travel_request(raw)
    assert req.preferences == ["美食"]
    assert any("蹦迪" in n for n in notices)


def test_evening_slot_requires_content():
    with pytest.raises(ValidationError):
        EveningSlot()
    EveningSlot(text="自由活动")


def test_itinerary_daily_hours_cap(chengdu_itin):
    itin, _ = chengdu_itin
    data = itin.model_dump()
    # 把第 1 天两个景点都换成 8 小时长项 → 总时长超限必须被拒
    day0 = data["days"][0]
    for slot in ("morning", "afternoon"):
        a = dict(day0[slot]["attraction"])
        a["duration_hours"] = 8
        day0[slot]["attraction"] = a
    # 注意：替换后两天同景点会重复，但时长校验先触发（模型级）
    with pytest.raises(ValidationError):
        Itinerary.model_validate(data)


def test_envelope_shape():
    env = Envelope(
        msg_type="subtasks",
        sender="planner",
        receiver="info",
        payload={"tasks": []},
    )
    assert env.sender == "planner"


def test_travel_request_rejects_zero_budget():
    with pytest.raises(ValidationError):
        TravelRequest(destination="成都", budget=0)


def test_total_budget_converted_to_per_person():
    """用户说"总预算"→ 除以人数换算人均，并明确告知。"""
    raw = ExtractedRequest.model_validate(
        {"destination": "武汉", "days": 1, "budget": 100, "budget_total": True, "party_size": 2}
    )
    req, notices = build_travel_request(raw)
    assert req.budget == 50
    assert req.party_size == 2
    assert any("人均预算 50" in n for n in notices)
