"""Budget Agent：费用估算、合理性判定、调整建议（文档 4.3 / 7 / 8 章）。

全部是确定性代码，不调用 LLM——省成本、结果稳定、好测试。
"""

from __future__ import annotations

from .. import config
from ..schemas import (
    HOTEL_TIERS,
    BudgetReport,
    Itinerary,
    Suggestion,
    TravelRequest,
)


class BudgetAgent:
    def check(self, request: TravelRequest, itinerary: Itinerary) -> BudgetReport:
        breakdown = self.estimate(request, itinerary)
        total = sum(breakdown.values())
        if total > request.budget * config.OVER_THRESHOLD:
            status = "over"
        elif total < request.budget * config.UNDER_THRESHOLD:
            status = "under"
        else:
            status = "ok"

        suggestions: list[Suggestion] = []
        note: str | None = None
        if status == "over":
            suggestions = self._make_suggestions(request, itinerary, breakdown)
        elif status == "under":
            note = "预算充裕，可升级酒店档次或增加付费体验"
        else:
            note = "总费用与预算匹配良好"
        return BudgetReport(
            status=status,
            budget=request.budget,
            estimated_total=total,
            breakdown=breakdown,
            suggestions=suggestions,
            note=note,
        )

    # ---- 7.1 分项估算（人均） ----

    def estimate(self, request: TravelRequest, itinerary: Itinerary) -> dict[str, int]:
        days = request.days
        if request.departure_city:
            distance = config.intercity_distance_km(request.departure_city, request.destination)
            intercity = round(distance * config.INTERCITY_RATE_PER_KM * 2)  # 往返
        else:
            intercity = 0

        city_transport = config.CITY_TRANSPORT_PER_DAY * days
        lodging = itinerary.hotel.price_per_night * (days - 1)

        student = bool(request.student)
        food_cost = config.BREAKFAST_PER_DAY * days
        for day in itinerary.days:
            for r in day.restaurants():
                # 学生/穷游：餐饮按团购价估算
                food_cost += (r.groupon_price or r.price_per_person) if student else r.price_per_person

        # 学生/穷游：门票按学生票（半价）估算
        tickets = sum(
            (a.student_price if student and a.student_price is not None else a.price)
            for day in itinerary.days
            for a in day.attractions()
        )

        subtotal = intercity + city_transport + lodging + food_cost + tickets
        misc = round(subtotal * config.MISC_RATE)
        return {
            "transport_intercity": intercity,
            "transport_city": city_transport,
            "lodging": lodging,
            "food": food_cost,
            "tickets": tickets,
            "misc": misc,
        }

    # ---- 8.2 调整建议优先级（Budget 生成，Planner 执行） ----

    def _make_suggestions(
        self, request: TravelRequest, itinerary: Itinerary, breakdown: dict[str, int]
    ) -> list[Suggestion]:
        total = sum(breakdown.values())
        suggestions: list[Suggestion] = []

        # 优先级 1：住宿占比过高 → 降一档
        lodging_ratio = breakdown["lodging"] / total if total else 0
        tier_idx = HOTEL_TIERS.index(itinerary.hotel.tier)
        if lodging_ratio > config.LODGING_RATIO_THRESHOLD and tier_idx > 0:
            to_tier = HOTEL_TIERS[tier_idx - 1]
            suggestions.append(
                Suggestion(
                    action="downgrade_hotel",
                    from_tier=itinerary.hotel.tier,
                    to_tier=to_tier,
                    reason=f"住宿占总费用 {lodging_ratio:.0%}，建议降到{to_tier}",
                )
            )

        # 优先级 2：正餐偏贵 → 换平价特色店（穷游：餐费每天必花，比门票更伤预算）
        pricey = sorted(
            (
                r
                for day in itinerary.days
                for r in day.restaurants()
                if r.price_per_person > config.FOOD_MEAL_BUDGET
            ),
            key=lambda r: r.price_per_person,
            reverse=True,
        )
        for r in pricey[:2]:
            suggestions.append(
                Suggestion(
                    action="replace_expensive_restaurant",
                    target=r.name,
                    reason=f"人均 {r.price_per_person} 元偏贵，可换平价特色店",
                )
            )

        # 优先级 3：付费景点按票价降序，最多替换 2 个
        paid = sorted(
            (a for day in itinerary.days for a in day.attractions() if a.price > 0),
            key=lambda a: a.price,
            reverse=True,
        )
        for target in paid[: config.REPLACE_PAID_MAX]:
            suggestions.append(
                Suggestion(
                    action="replace_paid_attraction",
                    target=target.name,
                    reason=f"门票 {target.price} 元，可换免费景点",
                )
            )

        # 优先级 4：前面都没有可调整项仍超标 → 压缩每天景点数
        if not suggestions:
            suggestions.append(
                Suggestion(
                    action="reduce_daily_activities",
                    reason="压缩每天的景点数量以降低门票与交通开销",
                )
            )
        return suggestions[:3]
