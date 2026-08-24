"""Budget Agent 单测：分项估算公式、判定阈值、建议优先级。"""

from travel_planner.agents.budget import BudgetAgent
from travel_planner.schemas import (
    Attraction,
    DayPlan,
    EveningSlot,
    Hotel,
    Itinerary,
    ItineraryMeta,
    Meals,
    Restaurant,
    Slot,
    TravelRequest,
)

A_FREE = Attraction(name="免费公园", tags=["公园", "免费"], price=0, duration_hours=2.0,
                    indoor=False, rating=4.5, area="青羊区")
A_PAID60 = Attraction(name="付费甲", tags=["人文"], price=60, duration_hours=2.0,
                      indoor=True, rating=4.5, area="青羊区")
A_PAID40 = Attraction(name="付费乙", tags=["博物馆"], price=40, duration_hours=2.0,
                      indoor=True, rating=4.4, area="锦江区")
R_LUNCH = Restaurant(name="午餐店", tags=["小吃"], price_per_person=50, meals=["lunch"],
                     rating=4.5, area="青羊区")
R_DINNER = Restaurant(name="晚餐店", tags=["川菜"], price_per_person=80, meals=["dinner"],
                      rating=4.5, area="青羊区")
H_COMFORT = Hotel(name="舒适酒店", tier="舒适型", price_per_night=400, area="青羊区",
                  tags=["近地铁"])
H_ECONOMY_CHEAP = Hotel(name="便宜旅店", tier="经济型", price_per_night=150, area="老城区",
                        tags=["交通方便"])
H_ECONOMY_HIGH = Hotel(name="贵点旅店", tier="经济型", price_per_night=280, area="老城区",
                       tags=["交通方便"])


def make_itinerary(hotel: Hotel, days: int = 2) -> Itinerary:
    plan_days = [
        DayPlan(
            day=i + 1,
            morning=Slot(attraction=A_PAID60 if i == 0 else A_PAID40),
            afternoon=Slot(attraction=A_FREE),
            evening=EveningSlot(text="自由活动"),
            meals=Meals(lunch=R_LUNCH, dinner=R_DINNER),
        )
        for i in range(days)
    ]
    return Itinerary(
        destination="南京", hotel=hotel, days=plan_days,
        meta=ItineraryMeta(),
    )


def make_request(**kw) -> TravelRequest:
    base = dict(destination="南京", days=2, budget=1000, departure_city=None)
    base.update(kw)
    return TravelRequest(**base)


def test_estimate_breakdown_exact():
    itin = make_itinerary(H_COMFORT)
    bd = BudgetAgent().estimate(make_request(departure_city="上海"), itin)
    # 上海→南京 300km × 0.45 × 往返
    assert bd["transport_intercity"] == round(300 * 0.45 * 2)
    assert bd["transport_city"] == 50 * 2
    assert bd["lodging"] == 400 * 1  # days-1 晚
    assert bd["food"] == 15 * 2 + (50 + 80) * 2  # 早餐 + 午晚餐
    assert bd["tickets"] == 60 + 40 + 0 + 0
    subtotal = sum(bd[k] for k in ("transport_intercity", "transport_city",
                                   "lodging", "food", "tickets"))
    assert bd["misc"] == round(subtotal * 0.10)
    assert sum(bd.values()) == subtotal + bd["misc"]


def test_status_thresholds():
    agent = BudgetAgent()
    itin = make_itinerary(H_ECONOMY_HIGH)  # 便宜组合：total ≈ 150+100+130+260+100+misc
    total = agent.check(make_request(budget=200), itin).estimated_total
    report = agent.check(make_request(budget=int(total / 1.06)), itin)  # 超阈值 → over
    assert report.status == "over"
    report = agent.check(make_request(budget=int(total * 3)), itin)  # 远超预算 → under
    assert report.status == "under"
    report = agent.check(make_request(budget=round(total / 0.8)), itin)  # 偏差 ≤15% → ok
    assert report.status == "ok"


def test_over_suggestions_priority():
    """住宿占比 >40% 且有付费景点 → 降档建议 + 两条替换建议。"""
    agent = BudgetAgent()
    itin = make_itinerary(H_COMFORT)  # 400/晚 占比高
    request = make_request(budget=500)  # 明显不够 → over
    report = agent.check(request, itin)
    assert report.status == "over"
    actions = [s.action for s in report.suggestions]
    assert actions[0] == "downgrade_hotel" or any(
        s.action == "downgrade_hotel" for s in report.suggestions
    )
    replace_targets = [s.target for s in report.suggestions if s.action == "replace_paid_attraction"]
    assert len(replace_targets) <= 2
    if replace_targets:  # 按票价降序：贵的先换
        assert replace_targets[0] == "付费甲"


def test_no_downgrade_when_already_economy():
    agent = BudgetAgent()
    itin = make_itinerary(H_ECONOMY_HIGH)  # 已是经济型且占比不高时不应降档
    request = make_request(budget=250)
    report = agent.check(request, itin)
    assert report.status in ("over", "ok")
    downgrades = [s for s in report.suggestions if s.action == "downgrade_hotel"]
    # 经济型已是最底档，不允许再降
    assert not downgrades


def test_under_note():
    agent = BudgetAgent()
    itin = make_itinerary(H_ECONOMY_CHEAP)
    report = agent.check(make_request(budget=9000), itin)
    assert report.status == "under"
    assert report.note and "升级" in report.note
