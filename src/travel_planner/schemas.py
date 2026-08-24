"""第 5 章全部 schema 的 pydantic 定义。

模型即文档：字段与《技术设计文档》第 5 章一一对应。
Agent 间消息统一用 Envelope 信封包裹，payload 为对应模型的 dict。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---- 枚举常量（与文档 5.2 / 5.3 节一致）----

PREFERENCES = ["美食", "人文", "自然", "亲子", "购物", "夜生活", "小众"]
ATTRACTION_EXTRA_TAGS = ["免费", "街区", "公园", "博物馆", "观景", "户外"]
HOTEL_TIERS = ["经济型", "舒适型", "高档型"]
WEATHER_CONDITIONS = ["晴", "多云", "阴", "小雨", "大雨"]

MsgType = Literal[
    "subtasks", "info_result", "itinerary", "budget_report", "adjust_request"
]
Party = Literal["planner", "info", "budget", "user", "orchestrator"]


# ---- 5.1/5.2 用户输入与需求 ----


class ExtractedRequest(BaseModel):
    """LLM 参数抽取的原始输出。字段允许为 null，由 Planner 补默认值后转 TravelRequest。"""

    model_config = ConfigDict(extra="ignore")

    destination: str | None = None
    days: int | None = None
    budget: int | None = None
    departure_city: str | None = None
    date: str | None = None
    preferences: list[str] | None = None
    party_size: int | None = None

    @field_validator("date")
    @classmethod
    def _check_date(cls, v: str | None) -> str | None:
        if v is not None and len(v) != 10:
            raise ValueError(f"date 须为 YYYY-MM-DD 格式，收到 {v!r}")
        return v


class TravelRequest(BaseModel):
    """规范化后的旅行需求（5.2 节）。destination 必填；days 钳制在 [1,7]。"""

    destination: str
    days: int = Field(default=3, ge=1, le=7)
    budget: int = Field(default=3000, gt=0)
    departure_city: str | None = None
    date: str | None = None
    preferences: list[str] = Field(default_factory=list)
    party_size: int = Field(default=1, ge=1)


def build_travel_request(raw: ExtractedRequest) -> tuple[TravelRequest, list[str]]:
    """从抽取结果构造 TravelRequest。

    返回 (request, notices)：notices 是需要告知用户的提示（如天数越界被钳制、
    非法偏好被丢弃）。
    """
    notices: list[str] = []
    days = raw.days or 3
    if raw.days is not None and not (1 <= raw.days <= 7):
        clamped = min(max(raw.days, 1), 7)
        notices.append(f"游玩天数 {raw.days} 超出范围，已按 {clamped} 天处理")
        days = clamped
    budget = raw.budget or 3000
    prefs = [p for p in (raw.preferences or []) if p in PREFERENCES]
    dropped = set(raw.preferences or []) - set(prefs)
    if dropped:
        notices.append(f"已忽略不支持的偏好：{'、'.join(sorted(dropped))}")
    req = TravelRequest(
        destination=raw.destination.strip() if raw.destination else "",
        days=days,
        budget=budget,
        departure_city=raw.departure_city,
        date=raw.date,
        preferences=prefs,
        party_size=max(raw.party_size or 1, 1),
    )
    return req, notices


# ---- 5.3 子任务与工具结果 ----


class SubTaskFilters(BaseModel):
    preferences: list[str] = Field(default_factory=list)
    indoor_only: bool = False


class SubTask(BaseModel):
    task_id: str
    type: Literal["attractions", "food", "hotels"]
    destination: str
    count: int
    filters: SubTaskFilters = Field(default_factory=SubTaskFilters)


class Attraction(BaseModel):
    name: str
    tags: list[str] = Field(default_factory=list)
    price: int = Field(ge=0)
    duration_hours: float = Field(gt=0)
    indoor: bool
    rating: float = Field(ge=4.0, le=5.0)
    area: str


class Restaurant(BaseModel):
    name: str
    tags: list[str] = Field(default_factory=list)
    price_per_person: int = Field(gt=0)
    meals: list[Literal["lunch", "dinner"]]
    rating: float = Field(ge=4.0, le=5.0)
    area: str


class Hotel(BaseModel):
    name: str
    tier: Literal["经济型", "舒适型", "高档型"]
    price_per_night: int = Field(gt=0)
    area: str
    tags: list[str] = Field(default_factory=list)


class Weather(BaseModel):
    city: str
    date: str | None = None
    condition: Literal["晴", "多云", "阴", "小雨", "大雨"]
    temp_low: int
    temp_high: int
    rain: bool


class InfoResult(BaseModel):
    task_id: str
    type: Literal["attractions", "food", "hotels"]
    source: Literal["local", "fallback"]
    data: list[dict[str, Any]]
    error: str | None = None

    def attractions(self) -> list[Attraction]:
        return [Attraction.model_validate(d) for d in self.data]

    def restaurants(self) -> list[Restaurant]:
        return [Restaurant.model_validate(d) for d in self.data]

    def hotels(self) -> list[Hotel]:
        return [Hotel.model_validate(d) for d in self.data]


# ---- 5.4 行程 ----


class Slot(BaseModel):
    """上午/下午时段：必须安排一个景点。"""

    attraction: Attraction


class EveningSlot(BaseModel):
    """晚间：自由活动文本、餐厅（夜市）或第三个景点，至少其一。"""

    text: str | None = None
    restaurant: Restaurant | None = None
    attraction: Attraction | None = None

    @model_validator(mode="after")
    def _non_empty(self) -> EveningSlot:
        if self.text is None and self.restaurant is None and self.attraction is None:
            raise ValueError("evening 至少要有 text / restaurant / attraction 之一")
        return self


class Meals(BaseModel):
    lunch: Restaurant
    dinner: Restaurant


class DayPlan(BaseModel):
    day: int
    date: str | None = None
    weather: Weather | None = None
    morning: Slot
    afternoon: Slot
    evening: EveningSlot
    meals: Meals

    def attractions(self) -> list[Attraction]:
        slots = [self.morning.attraction, self.afternoon.attraction]
        if self.evening.attraction is not None:
            slots.append(self.evening.attraction)
        return slots

    def restaurants(self) -> list[Restaurant]:
        used = [self.meals.lunch, self.meals.dinner]
        if self.evening.restaurant is not None:
            used.append(self.evening.restaurant)
        return used

    def total_hours(self) -> float:
        return sum(a.duration_hours for a in self.attractions())


MAX_DAILY_HOURS = 8


class Itinerary(BaseModel):
    destination: str
    hotel: Hotel
    days: list[DayPlan]
    meta: ItineraryMeta

    @model_validator(mode="after")
    def _daily_hours(self) -> Itinerary:
        # 文档 5.4：单日景点总时长 ≤ 8 小时。放在模型里保证 LLM 输出、
        # 降级编排、人工替换三条路径共用同一条硬约束。
        for d in self.days:
            if d.total_hours() > MAX_DAILY_HOURS:
                raise ValueError(
                    f"第 {d.day} 天景点总时长 {d.total_hours()} 小时，超过 {MAX_DAILY_HOURS} 小时上限"
                )
        return self


class ItineraryMeta(BaseModel):
    data_source: Literal["local", "fallback"] = "local"
    adjust_rounds: int = 0
    degraded: bool = False


# 允许前向引用（Itinerary 在 Meta 之后定义更直观，这里保持文档顺序）
Itinerary.model_rebuild()


# ---- 5.5 预算报告 ----


SuggestionAction = Literal[
    "downgrade_hotel", "replace_paid_attraction", "reduce_daily_activities", "none"
]


class Suggestion(BaseModel):
    action: SuggestionAction
    from_tier: str | None = None
    to_tier: str | None = None
    target: str | None = None
    reason: str | None = None


BREAKDOWN_KEYS = [
    "transport_intercity",
    "transport_city",
    "lodging",
    "food",
    "tickets",
    "misc",
]


class BudgetReport(BaseModel):
    status: Literal["ok", "over", "under"]
    budget: int
    estimated_total: int
    breakdown: dict[str, int]
    suggestions: list[Suggestion] = Field(default_factory=list)
    note: str | None = None

    @model_validator(mode="after")
    def _breakdown_keys(self) -> BudgetReport:
        missing = [k for k in BREAKDOWN_KEYS if k not in self.breakdown]
        if missing:
            raise ValueError(f"breakdown 缺少分项：{missing}")
        return self


# ---- 统一消息信封 ----


class Envelope(BaseModel):
    msg_type: MsgType
    sender: Party
    receiver: Party
    payload: dict[str, Any]


def wrap(msg_type: MsgType, sender: Party, receiver: Party, payload_model: BaseModel) -> Envelope:
    return Envelope(
        msg_type=msg_type,
        sender=sender,
        receiver=receiver,
        payload=payload_model.model_dump(),
    )
