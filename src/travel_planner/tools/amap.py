"""高德地图 Web 服务 API 客户端 + POI→schema 映射 + 免费配额控制（文档第 6 章）。

三个接口：
- `place/text`  按类型/关键词搜索景点、餐厅、酒店
- `geocode/geo` 城市名 → adcode（天气接口需要，进程内缓存）
- `weather/weatherInfo` adcode → 逐日天气预报（约 4 天）

配额控制（QuotaTracker）：每个服务每日计数，持久化到项目根 `.amap_quota.json`；
任一服务超限抛 `QuotaExhaustedError`，由 InfoAgent 特判上抛、CLI 停止整个程序。
节流：两次调用间隔 ≥ `AMAP_CALL_INTERVAL`，防个人 Key 的 QPS 限流。

价格说明（诚实估算）：高德 POI 接口只有 `biz_ext.rating`（评分）与 `biz_ext.cost`
（餐饮人均），没有门票价与房价。门票按 keytag/type 词表估算（未命中付费词视为
免费倾向），酒店按品牌/类型词表定档位、价格取配置档位区间中点——均为估算值，
行程单与搜索结果会标注"价格估算"。
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import date
from pathlib import Path
from typing import Any

from travel_planner import config


# ---- 异常类型 ----

class AmapError(Exception):
    """高德在线数据源错误（网络/接口/业务）。"""


class AmapConfigError(AmapError):
    """未配置 Key 等配置问题。"""


class AmapAuthError(AmapError):
    """Key 无效/过期/被限制：不重试，走兜底。"""


class AmapRateLimitError(AmapError):
    """QPS/请求过于频繁：可重试，仍失败走兜底。"""


class QuotaExhaustedError(AmapError):
    """免费配额用完：停止整个程序，不走兜底（用户要求）。"""


# ============================================================================
# 免费配额追踪
# ============================================================================


class QuotaTracker:
    """按服务每日计数，持久化到 JSON；日期变化自动清零。

    计数在进程外（文件）也生效：跨重启累计当日用量，防超额调用高德。
    """

    def __init__(self, path: str | Path, limits: dict[str, int], today: date | None = None) -> None:
        self.path = Path(path)
        self.limits = limits
        self.today = today or date.today()
        self._data: dict[str, dict[str, int]] = self._load()

    def _load(self) -> dict[str, dict[str, int]]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self) -> None:
        try:
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass  # 写不进去不影响运行，仅丢失计数

    def _bucket(self) -> dict[str, int]:
        day = str(self.today)
        bucket = self._data.setdefault(day, {})
        self._data[day] = bucket
        return bucket

    def count(self, service: str) -> int:
        return self._bucket().get(service, 0)

    def check(self, service: str) -> None:
        """超限抛 QuotaExhaustedError（供每次请求前调用）。"""
        limit = self.limits.get(service)
        if limit is None:
            return
        used = self.count(service)
        if used >= limit:
            raise QuotaExhaustedError(
                f"高德免费配额已用完（{service} 今日 {used}/{limit}）"
            )

    def record(self, service: str) -> None:
        self._bucket()[service] = self.count(service) + 1
        self._save()

    def exhausted_services(self) -> list[str]:
        """返回今日已达上限的服务名列表（供启动检查）。"""
        return [s for s, lim in self.limits.items() if self.count(s) >= lim]


# ============================================================================
# 高德客户端
# ============================================================================


class AmapClient:
    def __init__(
        self,
        key: str | None = None,
        quota: QuotaTracker | None = None,
        base_url: str | None = None,
    ) -> None:
        self.key = key or config.AMAP_KEY
        self.base_url = base_url or config.AMAP_BASE_URL
        self.quota = quota or QuotaTracker(config.AMAP_QUOTA_FILE, config.amap_daily_limits())
        self._geocode_cache: dict[str, str] = {}
        self._last_call_ts = 0.0

    # ---- 底层请求 ----

    async def _throttle(self) -> None:
        wait = config.AMAP_CALL_INTERVAL - (time.monotonic() - self._last_call_ts)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_call_ts = time.monotonic()

    async def _request(self, service: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
        self.quota.check(service)  # 超限 → QuotaExhaustedError，上抛停止程序
        await self._throttle()
        import httpx  # 惰性导入：httpx 较重，不进启动路径

        try:
            async with httpx.AsyncClient(timeout=config.TOOL_TIMEOUT_SECONDS) as client:
                resp = await client.get(
                    f"{self.base_url}/{path}", params={**params, "key": self.key}
                )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as e:
            raise AmapError(f"高德请求失败({service}): {e}") from e
        self.quota.record(service)  # 只要发出了请求就计数
        if str(data.get("status")) != "1":
            self._raise_api_error(data, service)
        return data

    def _raise_api_error(self, data: dict[str, Any], service: str) -> None:
        info = str(data.get("info") or "")
        code = str(data.get("infocode") or "")
        if code in ("10001", "10002", "10008", "10012", "10013") or "key" in info.lower():
            raise AmapAuthError(f"高德 Key 无效或受限({service}): {info}")
        if code in ("10003", "10004", "10019", "10020") or "qps" in info.lower() or "频繁" in info:
            raise AmapRateLimitError(f"高德请求过于频繁({service}): {info}")
        raise AmapError(f"高德接口错误({service}): {info}")

    # ---- 业务接口 ----

    async def geocode(self, city: str) -> str:
        """城市名 → adcode（天气接口需要）。进程内缓存避免重复计数。"""
        if city in self._geocode_cache:
            return self._geocode_cache[city]
        data = await self._request("geocode", "geocode/geo", {"address": city})
        codes = data.get("geocodes") or []
        if not codes or not codes[0].get("adcode"):
            raise AmapError(f"无法解析城市 adcode: {city}")
        adcode = str(codes[0]["adcode"])
        self._geocode_cache[city] = adcode
        return adcode

    async def search_pois(
        self,
        city: str,
        *,
        keywords: str | None = None,
        types: str | None = None,
        offset: int = 25,
    ) -> list[dict[str, Any]]:
        """POI 搜索。keywords 与 types 二选一（接口约束），offset ≤ 25。"""
        params: dict[str, Any] = {
            "city": city,
            "citylimit": "true",
            "offset": str(min(offset, 25)),
            "extensions": "all",
        }
        if keywords:
            params["keywords"] = keywords
        else:
            params["types"] = types or "风景名胜"
        data = await self._request("search", "place/text", params)
        return data.get("pois") or []

    async def forecast(self, adcode: str) -> list[dict[str, Any]]:
        """adcode → 逐日预报（casts，约 4 天）。"""
        data = await self._request(
            "weather", "weather/weatherInfo", {"city": adcode, "extensions": "all"}
        )
        forecasts = data.get("forecasts") or []
        if not forecasts:
            raise AmapError("天气接口无预报数据")
        return forecasts[0].get("casts") or []


_client: AmapClient | None = None


def make_amap_client() -> AmapClient:
    """进程内单例：共享地理编码缓存与节流器。未配置 Key 抛 AmapConfigError。"""
    global _client
    if _client is None:
        if not config.AMAP_KEY:
            raise AmapConfigError("未配置 AMAP_KEY，请在 .env 设置高德开放平台 Key")
        _client = AmapClient()
    return _client


def reset_amap_client() -> None:
    global _client
    _client = None


# ============================================================================
# POI → schema dict 映射（词表启发式，离线可测）
# ============================================================================


def _biz_ext(poi: dict[str, Any]) -> dict[str, Any]:
    ext = poi.get("biz_ext")
    return ext if isinstance(ext, dict) else {}


def _ext_float(poi: dict[str, Any], key: str) -> float | None:
    """biz_ext 里的数值字段：可能是 "102.00" 字符串、[] 或缺失。"""
    try:
        v = float(_biz_ext(poi).get(key))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _rating(value: Any, default: float) -> float:
    try:
        r = float(value)
    except (TypeError, ValueError):
        r = 0.0
    if not (0 < r <= 5):
        r = default
    return round(min(max(r, 4.0), 5.0), 1)  # schema 要求评分 ≥4.0


# ---- 景点 ----

# 付费倾向词表：命中即按估算价，未命中视为免费倾向（price=0 且 tag 免费）。
# 注意顺序：具体词在前（"水上乐园"先于"乐园"命中），价格取首个匹配。
PAID_PRICE_MAP: list[tuple[str, int]] = [
    ("水上乐园", 100), ("游乐园", 100), ("欢乐谷", 100),
    ("海洋馆", 100), ("海洋公园", 100), ("乐园", 80),
    ("滑雪", 150), ("温泉", 150), ("漂流", 100), ("演出", 120),
    ("动物园", 30), ("熊猫", 55), ("摩天轮", 60), ("索道", 80), ("缆车", 80),
    ("5A", 100), ("塔", 50), ("古镇", 50),
]


def estimate_attraction_price(name: str, keytag: str, type_: str) -> int:
    text = f"{name} {keytag} {type_}"
    for word, price in PAID_PRICE_MAP:
        if word in text:
            return price
    return 0


def estimate_duration_hours(text: str) -> float:
    for kw, h in (
        ("博物馆", 2.0), ("科技馆", 2.5), ("美术馆", 2.0), ("动物园", 3.0),
        ("游乐园", 4.0), ("水上乐园", 4.0), ("公园", 2.5), ("广场", 1.5),
        ("步行街", 2.0), ("街区", 2.0), ("湖", 3.0), ("山", 3.5), ("景区", 3.5),
    ):
        if kw in text:
            return h
    return 2.0


INDOOR_KEYWORDS = (
    "博物馆", "科技馆", "美术馆", "艺术馆", "图书馆", "纪念馆",
    "展览馆", "剧院", "影院", "海洋馆",
)


def is_indoor(text: str) -> bool:
    return any(k in text for k in INDOOR_KEYWORDS)


TAG_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("美食", ("火锅", "小吃", "美食", "餐厅", "咖啡", "甜品", "酒吧", "夜市", "川菜", "烧烤", "面")),
    ("人文", ("博物馆", "历史", "古迹", "寺庙", "教堂", "故居", "纪念馆", "遗址", "书院", "名人", "祠堂", "碑")),
    ("自然", ("公园", "湿地", "植物园", "动物园", "海洋", "瀑布", "森林", "山", "湖", "江", "河")),
    ("亲子", ("动物园", "游乐园", "儿童", "科技馆", "海洋馆", "水上乐园", "亲子")),
    ("购物", ("购物", "商业街", "步行街", "商场", "太古里", "市场", "百货")),
    ("夜生活", ("酒吧", "夜市", "夜景", "夜游", "演出", "酒吧街")),
    ("小众", ("小众", "秘境", "古镇")),
    ("公园", ("公园",)),
    ("博物馆", ("博物馆", "科技馆", "美术馆", "纪念馆", "艺术馆", "图书馆")),
    ("街区", ("街区", "步行街", "古街", "老街", "古镇")),
    ("观景", ("观景", "塔", "山顶", "天台", "摩天轮", "索道", "缆车")),
    ("户外", ("户外", "山", "湖", "公园", "湿地", "动物园", "徒步")),
]


def extract_tags(text: str) -> list[str]:
    return [tag for tag, kws in TAG_RULES if any(k in text for k in kws)]


def map_attraction(poi: dict[str, Any]) -> dict[str, Any]:
    name = str(poi.get("name") or "未知景点")
    keytag = str(poi.get("keytag") or "")
    type_ = str(poi.get("type") or "")
    text = f"{name} {keytag} {type_}"
    price = estimate_attraction_price(name, keytag, type_)
    tags = extract_tags(text)
    if price == 0:
        tags.append("免费")
    else:
        tags.append("价格估算")
    return {
        "name": name,
        "tags": tags,
        "price": price,
        "duration_hours": estimate_duration_hours(text),
        "indoor": is_indoor(text),
        "rating": _rating(_biz_ext(poi).get("rating"), 4.5),
        "area": str(poi.get("adname") or poi.get("business_area") or ""),
    }


# ---- 餐厅 ----

# 高德会把带餐饮的宾馆/酒店也标成"餐饮服务"，混进餐厅候选；
# 这些"酒店餐厅"不是市井美食，从餐厅池过滤掉。
HOTELISH_WORDS = (
    "酒店", "宾馆", "饭店", "旅舍", "青年旅舍", "客栈", "民宿",
    "公寓", "会所", "大厦", "写字楼", "商务楼",
)


def is_hotelish(poi: dict[str, Any]) -> bool:
    text = f"{poi.get('name') or ''} {poi.get('type') or ''}"
    return any(w in text for w in HOTELISH_WORDS)


def map_restaurant(poi: dict[str, Any]) -> dict[str, Any]:
    name = str(poi.get("name") or "未知餐厅")
    cost = _ext_float(poi, "cost") or 60  # 缺失人均按 60 元估算
    text = f"{name} {poi.get('keytag') or ''} {poi.get('type') or ''}"
    tags = extract_tags(text)
    if "美食" not in tags:
        tags.insert(0, "美食")
    return {
        "name": name,
        "tags": tags,
        "price_per_person": round(cost),
        "meals": ["lunch", "dinner"],  # 在线数据无餐段信息，默认午晚餐都供
        "rating": _rating(_biz_ext(poi).get("rating"), 4.3),
        "area": str(poi.get("business_area") or poi.get("adname") or ""),
    }


# ---- 酒店 ----

HIGH_END_WORDS = (
    "五星", "豪华", "皇冠", "万豪", "希尔顿", "香格里拉", "丽思", "铂尔曼",
    "洲际", "瑞吉", "安缦", "凯宾斯基", "威斯汀", "四季酒店", "君悦",
)
ECONOMY_WORDS = (
    "青年旅舍", "青旅", "客栈", "民宿", "快捷", "如家", "汉庭", "7天",
    "七天", "锦江之星", "速8", "速八", "莫泰", "招待所",
)


def map_hotel(poi: dict[str, Any]) -> dict[str, Any]:
    name = str(poi.get("name") or "未知酒店")
    keytag = str(poi.get("keytag") or "")
    text = f"{name} {keytag}"
    if any(w in text for w in HIGH_END_WORDS):
        tier = "高档型"
    elif any(w in text for w in ECONOMY_WORDS):
        tier = "经济型"
    else:
        tier = "舒适型"
    lo, hi = config.HOTEL_TIER_PRICE_RANGE[tier]
    return {
        "name": name,
        "tier": tier,
        "price_per_night": (lo + hi) // 2,  # 档位区间中点，估算价
        "area": str(poi.get("business_area") or poi.get("adname") or ""),
        "tags": ["价格估计"],
    }


# ---- 天气 ----

def map_weather_condition(weather_text: str) -> str:
    """高德天气文本 → schema 的 5 种 condition。"""
    t = weather_text or ""
    if "暴雨" in t or "大雨" in t:
        return "大雨"
    if "雨" in t:
        return "小雨"
    if "多云" in t or "云" in t:
        return "多云"
    if "阴" in t:
        return "阴"
    return "晴"


def _to_int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def make_weather_from_cast(city: str, date_: str | None, cast: dict[str, Any]) -> dict[str, Any]:
    """一条高德 casts → Weather 兼容 dict。"""
    text = f"{cast.get('dayweather') or ''}{cast.get('nightweather') or ''}"
    condition = map_weather_condition(text)
    high = _to_int(cast.get("daytemp"), 25)
    low = _to_int(cast.get("nighttemp"), 18)
    if low > high:
        low, high = high, low
    return {
        "city": city,
        "date": date_,
        "condition": condition,
        "temp_low": low,
        "temp_high": high,
        "rain": condition in ("小雨", "大雨"),
    }
