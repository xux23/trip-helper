"""Planner Agent 单测：抽取重试、子任务拆解、编排校验与降级、建议执行。"""

import pytest

from travel_planner.schemas import TravelRequest
from travel_planner.agents.planner import (
    ExtractError,
    MissingDestination,
    Planner,
    parse_json_text,
    template_compose,
)
from travel_planner.schemas import EveningSlot, Restaurant, Suggestion
from tests.conftest import (
    StubLLM,
    extract_response,
    fetch_chengdu_state,
    make_request,
    run,
)


# ---- 参数抽取 ----

def test_extract_success():
    llm = StubLLM([extract_response(preferences=["美食"])])
    outcome = run(Planner(llm).extract_request("国庆去成都玩3天，预算3000，喜欢美食"))
    assert outcome.request.destination == "成都"
    assert outcome.request.preferences == ["美食"]
    assert outcome.missing_fields == []
    call = llm.calls[0]
    assert "旅行需求解析器" in call["system"]
    assert call["temperature"] == 0
    assert call["max_tokens"] == 500  # prompt设计.md 通用参数


def test_extract_missing_destination_raises():
    llm = StubLLM([extract_response(destination=None)])
    with pytest.raises(MissingDestination):
        run(Planner(llm).extract_request("想去玩几天"))


def test_re_extract_with_destination():
    llm = StubLLM([
        extract_response(days=None, destination=None),
        extract_response(days=None),  # 补充目的地后仍缺天数 → CLI 追问
    ])
    outcome = run(Planner(llm).re_extract_with_destination("想去玩几天", "杭州"))
    assert outcome.request.destination == "杭州"
    assert "days" in outcome.missing_fields


def test_extract_retry_on_bad_json():
    llm = StubLLM(["这不是JSON", extract_response()])
    outcome = run(Planner(llm).extract_request("去成都3天"))
    assert outcome.request.destination == "成都"
    assert len(llm.calls) == 2
    assert "不合法" in llm.calls[1]["user"]  # 错误信息拼回 Prompt（prompt设计.md 1.3）


def test_extract_fails_after_two_attempts():
    llm = StubLLM(["bad1", "bad2"])
    with pytest.raises(ExtractError):
        run(Planner(llm).extract_request("去成都3天"))


# ---- 子任务拆解 ----

def test_make_subtasks_counts():
    tasks = Planner(None).make_subtasks(
        TravelRequest(
            destination="成都", days=3, preferences=["美食"]
        )
    )
    by_type = {t.type: t for t in tasks}
    assert by_type["attractions"].count == 9  # days × 3
    assert by_type["food"].count == 6  # days × 2
    assert by_type["hotels"].count == 5  # 固定 5
    assert by_type["attractions"].filters.preferences == ["美食"]


# ---- 行程编排 ----

def test_compose_llm_path_canonicalizes():
    request, results, weather, pools = fetch_chengdu_state()
    template_itin = template_compose(request, pools, weather)
    llm = StubLLM([template_itin.model_dump_json()])
    out = run(Planner(llm).compose_itinerary(request, results, weather))
    assert out.meta.degraded is False  # LLM 主路径成功
    assert out.meta.data_source == "online"
    pool_names = {a.name for a in pools.attractions}
    for day in out.days:
        for a in day.attractions():
            assert a.name in pool_names


def test_compose_degrades_on_llm_network_error():
    """编排阶段 LLM 网络/超时 → 降级模板编排，不崩流程。"""
    request, results, weather, pools = fetch_chengdu_state()

    class BoomLLM:
        model = "boom"

        async def chat(self, messages, *, temperature: float, max_tokens: int):
            raise RuntimeError("LLM 调用失败（已重试 1 次）：Request timed out.")

    itin = run(Planner(BoomLLM()).compose_itinerary(request, results, weather))
    assert itin.meta.degraded is True
    assert len(itin.days) == request.days
    for day in itin.days:
        assert day.meals.lunch and day.meals.dinner


def test_compose_rejects_fabricated_names_and_degrades():
    request, results, weather, pools = fetch_chengdu_state()
    fabricated = template_compose(request, pools, weather).model_copy(deep=True)
    fabricated.days[0].morning.attraction.name = "不存在的景点"
    bad_json = fabricated.model_dump_json()
    llm = StubLLM([bad_json, bad_json])  # 重试后仍非法
    itin = run(Planner(llm).compose_itinerary(request, results, weather))
    assert itin.meta.degraded is True
    assert len(llm.calls) == 2  # 降级不再调 LLM
    names = [a.name for d in itin.days for a in d.attractions()]
    assert len(names) == len(set(names))  # 降级产物无重复
    for day in itin.days:
        assert day.meals.lunch and day.meals.dinner


def test_parse_json_tolerates_markdown_fence():
    assert parse_json_text('```json\n{"a":1}\n```') == {"a": 1}


def test_template_hotel_budget_aware():
    """预算偏紧（人均每日 ≤ 阈值）模板默认经济型，宽松默认舒适型。"""
    req_tight = make_request(budget=600)  # 600/3 = 200 元/天
    _, _, weather_t, pools_t = fetch_chengdu_state(req_tight)
    itin_tight = template_compose(req_tight, pools_t, weather_t)
    assert itin_tight.hotel.tier == "经济型"

    req_ok = make_request(budget=3000)  # 3000/3 = 1000 元/天
    _, _, weather_o, pools_o = fetch_chengdu_state(req_ok)
    itin_ok = template_compose(req_ok, pools_o, weather_o)
    assert itin_ok.hotel.tier == "舒适型"


# ---- 建议执行 ----

def test_apply_downgrade_picks_cheapest_of_tier():
    request, results, weather, pools = fetch_chengdu_state()
    itin = template_compose(request, pools, weather)
    planner = Planner(None)
    planner._pools = pools
    out = planner.apply_suggestions(itin, [Suggestion(action="downgrade_hotel", to_tier="经济型")])
    economy = sorted((h for h in pools.hotels if h.tier == "经济型"), key=lambda h: h.price_per_night)
    assert out.hotel.name == economy[0].name  # 该档最低价（文档 8.2）


def test_apply_replace_paid_uses_free_candidate():
    request, results, weather, pools = fetch_chengdu_state()
    itin = template_compose(request, pools, weather)
    paid = next(a for a in pools.attractions if a.price > 0)
    planner = Planner(None)
    planner._pools = pools
    out = planner.apply_suggestions(itin, [Suggestion(action="replace_paid_attraction", target=paid.name)])
    new_names = {a.name for d in out.days for a in d.attractions()}
    assert paid.name not in new_names
    assert any(a.price == 0 for d in out.days for a in d.attractions()) or any(
        a.price < paid.price for d in out.days for a in d.attractions()
    )


def test_apply_replace_expensive_restaurant():
    """正餐换平价：同餐段更便宜的未用候选；没有更便宜的不换（别省过头）。"""
    request, results, weather, pools = fetch_chengdu_state()
    itin = template_compose(request, pools, weather).model_copy(deep=True)
    expensive = Restaurant(
        name="贵价餐厅", tags=["美食"], price_per_person=150, meals=["lunch"],
        rating=4.5, area="青羊区",
    )
    itin.days[0].meals.lunch = expensive

    planner = Planner(None)
    planner._pools = pools
    out = planner.apply_suggestions(
        itin, [Suggestion(action="replace_expensive_restaurant", target="贵价餐厅")]
    )
    new_lunch = out.days[0].meals.lunch
    assert new_lunch.name != "贵价餐厅"
    assert new_lunch.price_per_person < 150
    assert "lunch" in new_lunch.meals  # 同餐段兼容


def test_apply_reduce_removes_evening_attraction():
    request, results, weather, pools = fetch_chengdu_state()
    itin = template_compose(request, pools, weather)
    deep = itin.model_copy(deep=True)
    used = {a.name for d in deep.days for a in d.attractions()}
    spare = next(a for a in pools.attractions if a.name not in used)
    deep.days[0].evening = EveningSlot(attraction=spare)

    planner = Planner(None)
    planner._pools = pools
    out = planner.apply_suggestions(deep, [Suggestion(action="reduce_daily_activities")])
    assert out.days[0].evening.attraction is None
    assert out.days[0].evening.text
