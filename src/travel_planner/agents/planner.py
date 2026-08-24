"""Planner Agent（文档 4.1 节）：需求理解、子任务拆解、行程编排、执行预算调整。

全系统仅此处的两个方法调用 LLM（参数抽取、行程编排），
Prompt 定稿见 docs/prompt设计.md，修改 Prompt 先改文档。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date

from pydantic import ValidationError

from ..llm import LLMClient
from ..schemas import (
    Attraction,
    DayPlan,
    EveningSlot,
    ExtractedRequest,
    Hotel,
    InfoResult,
    Itinerary,
    ItineraryMeta,
    MAX_DAILY_HOURS,
    Meals,
    Restaurant,
    Suggestion,
    Slot,
    SubTask,
    SubTaskFilters,
    TravelRequest,
    Weather,
    build_travel_request,
)
from .info import build_dates

# ============================================================================
# Prompt A：参数抽取（prompt设计.md 1.1 节，逐字一致）
# ============================================================================

EXTRACT_SYSTEM_PROMPT = """你是一个旅行需求解析器。任务：从用户的一句话中抽取结构化字段。

今天是 {today}（用于换算"国庆""五一"等节日为具体日期）。

只输出一个 JSON 对象，不要输出任何解释、markdown 代码块标记或其他文字。

字段规则：
- destination：城市名。输入中没有明确城市时输出 null。
- days：游玩天数，整数。没有则输出 null（由代码填默认值 3）。
- budget：人均预算，整数，单位元。没有则输出 null（默认 3000）。"穷游"映射为 800。
- departure_city：出发城市。没有则输出 null。
- date：出发日期，格式 YYYY-MM-DD。节日换算为当年日期（国庆→10-01，五一→05-01）；只说月份按当月 1 号；没有则输出 null。
- preferences：偏好列表，只能从这些值里选：美食、人文、自然、亲子、购物、夜生活、小众、免费。没有则输出 []。
- party_size：同行人数，整数。"我们/情侣/两个人"等都算 2。默认 1。
- search_keyword：用户想要"搜索/查找/找某类景点"而不是生成完整行程时，输出搜索关键词（如"免费""博物馆""公园"），"免费"表示只看免费景点；其他情况输出 null。

示例：
输入：国庆和女朋友去成都玩3天，预算3000，喜欢吃火锅
输出：{{"destination":"成都","days":3,"budget":3000,"departure_city":null,"date":"2026-10-01","preferences":["美食"],"party_size":2,"search_keyword":null}}

输入：五一从武汉去长沙，带娃，2天
输出：{{"destination":"长沙","days":2,"budget":null,"departure_city":"武汉","date":"2026-05-01","preferences":["亲子"],"party_size":1,"search_keyword":null}}

输入：找成都免费的景点
输出：{{"destination":"成都","days":null,"budget":null,"departure_city":null,"date":null,"preferences":[],"party_size":1,"search_keyword":"免费"}}"""

EXTRACT_RETRY_TEMPLATE = """你上一次的输出不合法，校验错误：
{error}

请重新输出，只输出修正后的 JSON，不要有任何其他文字。"""

# ============================================================================
# Prompt B：行程编排（prompt设计.md 2.1 节，逐字一致）
# ============================================================================

COMPOSE_SYSTEM_PROMPT = """你是一名行程规划师。根据给定的候选数据和约束，编排一份按天的行程。

硬性规则（违反任何一条即为失败）：
1. 景点、餐厅、酒店只能从候选列表中选，原样复制字段，不得编造或改名。
2. 每天安排 morning 和 afternoon 两个时段的景点，evening 可以是夜市/自由活动文本或一家餐厅。
3. 每天选 2~3 个景点，全天景点 duration_hours 总和不超过 8。
4. 每天安排午餐 lunch 和晚餐 dinner 各一家餐厅，从候选中选，meals 字段须匹配（午餐选 meals 含 lunch 的）。
5. weather.rain 为 true 的天，优先选 indoor 为 true 的景点。
6. 同一天的景点尽量选相同的 area。
7. 与 preferences 标签匹配的候选优先安排。
8. 全程只选 1 家酒店。
9. 只输出一个 JSON 对象，不要输出任何解释或 markdown 标记。

输出 JSON 结构：
{
  "destination": "城市名",
  "hotel": {候选酒店对象，原样复制},
  "days": [
    {
      "day": 1,
      "date": "YYYY-MM-DD 或 null",
      "weather": {当天的天气对象，原样复制},
      "morning": {"attraction": {候选景点对象}},
      "afternoon": {"attraction": {候选景点对象}},
      "evening": {"text": "…"} 或 {"restaurant": {候选餐厅对象}},
      "meals": {"lunch": {候选餐厅对象}, "dinner": {候选餐厅对象}}
    }
  ],
  "meta": {"data_source": "local 或 fallback", "adjust_rounds": 0, "degraded": false}
}"""

COMPOSE_USER_TEMPLATE = """旅行需求：
{request}

候选景点（{n_attr}个）：
{attractions}

候选餐厅（{n_food}个）：
{restaurants}

候选酒店（{n_hotel}个）：
{hotels}

逐日天气：
{weather}"""

COMPOSE_RETRY_TEMPLATE = """你上一次的输出不合法，校验错误：
{error}

常见问题：使用了候选列表之外的条目、缺少 lunch/dinner、days 数量不对。
请重新输出完整的行程 JSON，只输出 JSON。"""


class ExtractError(Exception):
    """参数抽取最终失败（重试后仍不合法）。"""


class MissingDestination(ExtractError):
    """目的地缺失：不重试，交给 CLI 追问流程（文档 4.1 节）。"""

    def __init__(self, partial: ExtractedRequest) -> None:
        super().__init__("未能从输入中识别目的地")
        self.partial = partial


@dataclass
class ExtractionOutcome:
    request: TravelRequest
    missing_fields: list[str] = field(default_factory=list)  # days/budget 缺失时由 CLI 追问
    notices: list[str] = field(default_factory=list)


@dataclass
class CandidatePools:
    attractions: list[Attraction]
    restaurants: list[Restaurant]
    hotels: list[Hotel]


def parse_json_text(text: str) -> dict:
    """容忍 markdown 围栏：剥掉 ```json ...``` 再解析。"""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"输出中找不到 JSON 对象：{text[:80]!r}")
    return json.loads(cleaned[start : end + 1])


class Planner:
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm
        self._pools: CandidatePools | None = None

    # ---- 方法一：参数抽取（LLM） ----

    async def extract_request(self, user_input: str) -> ExtractionOutcome:
        raw = await self._extract_with_retry(user_input)
        return _to_outcome(raw)

    async def re_extract_with_destination(
        self, user_input: str, destination: str
    ) -> ExtractionOutcome:
        """追问到目的地后重新抽取一次，带上已知上下文（prompt设计.md 1.4 节）。"""
        supplemented = f"{user_input}\n（用户补充了目的地：{destination}，请据此重新抽取）"
        raw = await self._extract_with_retry(supplemented)
        if not raw.destination:
            raw = raw.model_copy(update={"destination": destination})
        return _to_outcome(raw)

    async def _extract_with_retry(self, user_message: str) -> ExtractedRequest:
        system = EXTRACT_SYSTEM_PROMPT.format(today=date.today().isoformat())
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_message},
        ]
        last_error: Exception | None = None
        for attempt in range(2):
            resp = await self.llm.chat(messages, temperature=0, max_tokens=500)
            try:
                return ExtractedRequest.model_validate(parse_json_text(resp.text))
            except (ValidationError, ValueError, json.JSONDecodeError) as e:
                last_error = e
                if attempt == 0:
                    messages = messages[:2] + [
                        {"role": "assistant", "content": resp.text},
                        {"role": "user", "content": EXTRACT_RETRY_TEMPLATE.format(error=str(e))},
                    ]
        raise ExtractError(f"参数抽取失败：{last_error}")

    # ---- 方法二：子任务拆解（代码，模板化） ----

    def make_subtasks(self, request: TravelRequest) -> list[SubTask]:
        filters = SubTaskFilters(preferences=list(request.preferences))
        return [
            SubTask(
                task_id="T1",
                type="attractions",
                destination=request.destination,
                count=request.days * 3,
                filters=filters,
            ),
            SubTask(task_id="T2", type="food", destination=request.destination, count=request.days * 2),
            SubTask(task_id="T3", type="hotels", destination=request.destination, count=5),
        ]

    # ---- 方法三：行程编排（LLM，失败降级为代码模板） ----

    async def compose_itinerary(
        self,
        request: TravelRequest,
        results: list[InfoResult],
        weather: list[Weather],
    ) -> Itinerary:
        pools = self._collect_pools(results)
        self._pools = pools  # 预算调整与人工替换要用同一份候选池
        data_source = "fallback" if any(r.source == "fallback" for r in results) else "online"

        user_msg = COMPOSE_USER_TEMPLATE.format(
            request=request.model_dump_json(indent=2),
            n_attr=len(pools.attractions),
            attractions=_dump(pools.attractions),
            n_food=len(pools.restaurants),
            restaurants=_dump(pools.restaurants),
            n_hotel=len(pools.hotels),
            hotels=_dump(pools.hotels),
            weather=_dump(weather),
        )
        messages: list[dict[str, str]] = [
            {"role": "system", "content": COMPOSE_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ]
        last_error: Exception | None = None
        for attempt in range(2):
            resp = await self.llm.chat(messages, temperature=0.3, max_tokens=4000)
            try:
                itinerary = Itinerary.model_validate(parse_json_text(resp.text))
                itinerary = self._canonicalize(itinerary, pools, request)
                # 这两个标记反映"本次编排"的实际路径，不信任输入里的值
                itinerary.meta.data_source = data_source
                itinerary.meta.adjust_rounds = 0
                itinerary.meta.degraded = False
                return itinerary
            except (ValidationError, ValueError) as e:
                last_error = e
                if attempt == 0:
                    messages = messages[:2] + [
                        {"role": "assistant", "content": resp.text},
                        {"role": "user", "content": COMPOSE_RETRY_TEMPLATE.format(error=str(e))},
                    ]
        assert last_error is not None
        # 降级：不再调 LLM，用确定性模板编排并标注（prompt设计.md 2.4 节）
        return template_compose(request, pools, weather, data_source=data_source)

    def _collect_pools(self, results: list[InfoResult]) -> CandidatePools:
        attractions: list[Attraction] = []
        restaurants: list[Restaurant] = []
        hotels: list[Hotel] = []
        for r in results:
            if r.type == "attractions":
                attractions.extend(r.attractions())
            elif r.type == "food":
                restaurants.extend(r.restaurants())
            else:
                hotels.extend(r.hotels())
        return CandidatePools(attractions=attractions, restaurants=restaurants, hotels=hotels)

    def _canonicalize(
        self, itinerary: Itinerary, pools: CandidatePools, request: TravelRequest
    ) -> Itinerary:
        """校验 LLM 输出只引用候选项，并用候选池原对象替换（防字段漂移影响算价）。"""
        errors: list[str] = []
        if len(itinerary.days) != request.days:
            errors.append(f"天数应为 {request.days} 天，实际 {len(itinerary.days)} 天")

        a_index = {a.name: a for a in pools.attractions}
        r_index = {r.name: r for r in pools.restaurants}
        hotel = next((h for h in pools.hotels if h.name == itinerary.hotel.name), None)
        if hotel is None:
            errors.append(f"酒店 {itinerary.hotel.name!r} 不在候选列表")
            hotel = itinerary.hotel

        used_attractions: set[str] = set()
        used_meals: set[str] = set()  # 午/晚餐全局唯一；晚间餐厅允许复用（夜市常与晚餐重合）
        new_days: list[DayPlan] = []

        def canon_attraction(attr: Attraction | None, where: str) -> Attraction | None:
            if attr is None:
                return None
            obj = a_index.get(attr.name)
            if obj is None:
                errors.append(f"{where}景点 {attr.name!r} 不在候选列表")
                return None
            if attr.name in used_attractions:
                errors.append(f"景点 {attr.name!r} 在行程中重复出现")
                return None
            used_attractions.add(attr.name)
            return obj

        def canon_restaurant(rest: Restaurant | None, where: str, unique: bool) -> Restaurant | None:
            if rest is None:
                return None
            obj = r_index.get(rest.name)
            if obj is None:
                errors.append(f"{where}餐厅 {rest.name!r} 不在候选列表")
                return None
            if unique:
                if rest.name in used_meals:
                    errors.append(f"餐厅 {rest.name!r} 作为正餐重复使用")
                    return None
                used_meals.add(rest.name)
            return obj

        for day in itinerary.days:
            where = f"第 {day.day} 天"
            morning = canon_attraction(day.morning.attraction, f"{where}上午")
            afternoon = canon_attraction(day.afternoon.attraction, f"{where}下午")
            evening_attraction = (
                canon_attraction(day.evening.attraction, f"{where}晚间")
                if day.evening.attraction is not None
                else None
            )
            lunch = canon_restaurant(day.meals.lunch, f"{where}", unique=True)
            dinner = canon_restaurant(day.meals.dinner, f"{where}", unique=True)
            evening_rest = canon_restaurant(day.evening.restaurant, f"{where}晚间", unique=False)

            if morning is None or afternoon is None or lunch is None or dinner is None:
                continue  # 错误已记录，最后统一抛出触发重试

            new_days.append(
                DayPlan(
                    day=day.day,
                    date=day.date,
                    weather=day.weather,
                    morning=Slot(attraction=morning),
                    afternoon=Slot(attraction=afternoon),
                    evening=EveningSlot(
                        text=day.evening.text,
                        restaurant=evening_rest,
                        attraction=evening_attraction,
                    ),
                    meals=Meals(lunch=lunch, dinner=dinner),
                )
            )

        if errors:
            raise ValueError("；".join(errors[:8]))
        if len(new_days) != request.days:
            raise ValueError(f"有效天数不足：应为 {request.days} 天，实际 {len(new_days)} 天")

        return Itinerary(
            destination=request.destination,
            hotel=hotel,
            days=new_days,
            meta=itinerary.meta,
        )

    # ---- 方法四：执行预算调整（代码，确定性替换，不调 LLM） ----

    def apply_suggestions(self, itinerary: Itinerary, suggestions: list[Suggestion]) -> Itinerary:
        current = itinerary
        for s in suggestions:
            if s.action == "downgrade_hotel":
                current = self._apply_downgrade(current, s.to_tier)
            elif s.action == "replace_paid_attraction":
                current = self._apply_replace_attraction(current, s.target)
            elif s.action == "reduce_daily_activities":
                current = self._apply_reduce_activities(current)
        return current

    def _require_pools(self) -> CandidatePools:
        if self._pools is None:
            raise RuntimeError("候选池未初始化：请先调用 compose_itinerary")
        return self._pools

    def _apply_downgrade(self, itinerary: Itinerary, to_tier: str | None) -> Itinerary:
        if to_tier is None:
            return itinerary
        pools = self._require_pools()
        options = sorted(
            (h for h in pools.hotels if h.tier == to_tier and h.name != itinerary.hotel.name),
            key=lambda h: h.price_per_night,
        )
        if not options:
            return itinerary
        return self.replace_hotel(itinerary, options[0])  # 该档最低价（文档 8.2 节）

    def _apply_replace_attraction(self, itinerary: Itinerary, target: str | None) -> Itinerary:
        if target is None:
            return itinerary
        pools = self._require_pools()
        updated = itinerary.model_copy(deep=True)
        for day in updated.days:
            for slot_name in ("morning", "afternoon", "evening"):
                slot = getattr(day, slot_name)
                if slot.attraction is None or slot.attraction.name != target:
                    continue
                raining = bool(day.weather and day.weather.rain)
                used = {a.name for d in updated.days for a in d.attractions()}
                used.discard(target)
                replacement = choose_replacement(
                    target_attr=slot.attraction,
                    candidates=pools.attractions,
                    used_names=used,
                    raining=raining,
                )
                if replacement is None:
                    continue
                slot.attraction = replacement
                return _revalidate(updated)
        return itinerary

    def _apply_reduce_activities(self, itinerary: Itinerary) -> Itinerary:
        """每天景点数压到 2 个：移除晚间的第三个景点（文档 8.2 节优先级 3）。"""
        changed = False
        updated = itinerary.model_copy(deep=True)
        for day in updated.days:
            if day.evening.attraction is not None:
                day.evening.attraction = None
                day.evening.text = "自由活动，可逛周边街区或夜市"
                changed = True
        return _revalidate(updated) if changed else itinerary

    # ---- FR7 人工调整辅助（同样只动结构，不调 LLM） ----

    def alternative_attractions(self, itinerary: Itinerary) -> list[Attraction]:
        pools = self._require_pools()
        used = {a.name for day in itinerary.days for a in day.attractions()}
        return sorted(
            (a for a in pools.attractions if a.name not in used),
            key=lambda a: (-a.rating, a.price),
        )

    def candidate_hotels(self) -> list[Hotel]:
        return sorted(self._require_pools().hotels, key=lambda h: (h.price_per_night, h.tier))

    def replace_attraction(
        self, itinerary: Itinerary, day_number: int, slot_name: str, replacement: Attraction
    ) -> Itinerary:
        updated = itinerary.model_copy(deep=True)
        day = next((d for d in updated.days if d.day == day_number), None)
        if day is None:
            raise ValueError(f"没有第 {day_number} 天")
        slot = getattr(day, slot_name, None)
        if slot is None or getattr(slot, "attraction", None) is None:
            raise ValueError(f"时段 {slot_name} 没有可替换的景点")
        slot.attraction = replacement
        return _revalidate(updated)

    def replace_hotel(self, itinerary: Itinerary, hotel: Hotel) -> Itinerary:
        updated = itinerary.model_copy(update={"hotel": hotel})
        return _revalidate(updated)


def _to_outcome(raw: ExtractedRequest) -> ExtractionOutcome:
    if not raw.destination:
        raise MissingDestination(raw)
    request, notices = build_travel_request(raw)
    missing = [f for f in ("days", "budget") if getattr(raw, f) is None]
    return ExtractionOutcome(request=request, missing_fields=missing, notices=notices)


# ============================================================================
# 模板化降级编排（prompt设计.md 2.4 节）
# ============================================================================


def template_compose(
    request: TravelRequest,
    pools: CandidatePools,
    weather: list[Weather],
    *,
    data_source: str = "online",
) -> Itinerary:
    """代码模板编排：按评分降序装箱、雨天优先室内、每天 2 景点 + 午晚餐各 1 家。"""
    if not pools.hotels or not pools.restaurants or not pools.attractions:
        raise ValueError("候选池为空，无法生成行程")
    dates = build_dates(request.date, request.days)
    ordered = sorted(pools.attractions, key=lambda a: (-a.rating, a.price))
    lunches = (
        sorted((r for r in pools.restaurants if "lunch" in r.meals), key=lambda r: -r.rating)
        or sorted(pools.restaurants, key=lambda r: -r.rating)
    )
    dinners = (
        sorted((r for r in pools.restaurants if "dinner" in r.meals), key=lambda r: -r.rating)
        or sorted(pools.restaurants, key=lambda r: -r.rating)
    )

    used_attractions: set[str] = set()

    def pick_day_attractions(day_index: int) -> list[Attraction]:
        raining = bool(day_index < len(weather) and weather[day_index].rain)
        remaining = [a for a in ordered if a.name not in used_attractions]
        if raining:  # 雨天优先室内（Prompt B 规则 5 同款约束）
            remaining = sorted(remaining, key=lambda a: 0 if a.indoor else 1)
        picked: list[Attraction] = []
        hours = 0.0
        for a in remaining:
            if len(picked) >= 2:
                break
            if hours + a.duration_hours <= MAX_DAILY_HOURS:
                picked.append(a)
                hours += a.duration_hours
        for a in sorted((x for x in remaining if x not in picked), key=lambda x: x.duration_hours):
            if len(picked) >= 2:
                break
            picked.append(a)  # 极端数据分布时保结构完整优先于时长软约束
        used_attractions.update(a.name for a in picked[:2])
        return picked

    comfort = sorted(
        (h for h in pools.hotels if h.tier == "舒适型"), key=lambda h: h.price_per_night
    )
    hotel = comfort[0] if comfort else min(pools.hotels, key=lambda h: h.price_per_night)

    used_restaurants: set[str] = set()

    def pick_meal(candidates: list[Restaurant]) -> Restaurant | None:
        for r in candidates:
            if r.name not in used_restaurants:
                used_restaurants.add(r.name)
                return r
        return None

    days: list[DayPlan] = []
    for i in range(request.days):
        picks = pick_day_attractions(i)
        w = weather[i] if i < len(weather) else None
        lunch = pick_meal(lunches) or lunches[i % len(lunches)]  # 池耗尽才循环复用
        dinner = pick_meal([r for r in dinners if r.name != lunch.name])
        if dinner is None:
            pool_d = [r for r in dinners if r.name != lunch.name]
            dinner = pool_d[i % len(pool_d)]
        days.append(
            DayPlan(
                day=i + 1,
                date=dates[i],
                weather=w,
                morning=Slot(attraction=picks[0]),
                afternoon=Slot(attraction=picks[-1]),
                evening=EveningSlot(text="自由活动，可逛周边街区或夜市"),
                meals=Meals(lunch=lunch, dinner=dinner),
            )
        )

    return Itinerary(
        destination=request.destination,
        hotel=hotel,
        days=days,
        meta=ItineraryMeta(data_source=data_source, adjust_rounds=0, degraded=True),
    )


def choose_replacement(
    target_attr: Attraction,
    candidates: list[Attraction],
    used_names: set[str],
    *,
    raining: bool,
) -> Attraction | None:
    """为付费景点挑替代：免费/低价优先 → 雨天室内优先 → 时长接近。"""
    options = [a for a in candidates if a.name not in used_names and a.name != target_attr.name]
    if not options:
        return None

    def sort_key(a: Attraction) -> tuple:
        rain_fit = 0 if a.indoor else 1
        return (a.price, rain_fit if raining else 0, abs(a.duration_hours - target_attr.duration_hours))

    return min(options, key=sort_key)


def _revalidate(itinerary: Itinerary) -> Itinerary:
    """结构性改动后重新过一遍模型校验（含每日 ≤8 小时硬约束）。"""
    return Itinerary.model_validate(itinerary.model_dump())


def _dump(models: list) -> str:
    return json.dumps([m.model_dump() for m in models], ensure_ascii=False, indent=2)
