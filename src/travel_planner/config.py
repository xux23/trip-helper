"""环境变量读取与全部魔法数字集中区（文档 10.3 节）。

所有规则里的数字（50元/天、10%机动、1.05/0.5 阈值、2轮上限等）
只出现在这里，便于评审与调整。
"""

from __future__ import annotations

import os


def _load_dotenv() -> None:
    """极小 .env 加载器：不引入第三方依赖。

    项目根目录的 .env 仅用于本地开发配置（已在 .gitignore 忽略）。
    环境变量已存在时以环境为准，不覆盖。
    """
    env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", ".env")
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip()
                if value and (value[0] in "\"'" and value[-1] == value[0]):
                    value = value[1:-1]
                if key and key not in os.environ:
                    os.environ[key] = value
    except FileNotFoundError:
        pass


_load_dotenv()

# ---- LLM 三件套：OpenAI 兼容服务商通用 ----

LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")

# ---- 故障注入开关（面试演示兜底用，文档 4.2 节）----

SIMULATE_TOOL_FAILURE = os.environ.get("SIMULATE_TOOL_FAILURE", "") == "1"

# 是否打印 Agent 间消息信封日志（文档 Q3：信封方便排查）
LOG_ENVELOPES = os.environ.get("LOG_ENVELOPES", "") == "1"


# ---- 工具层 ----

TOOL_TIMEOUT_SECONDS = 3.0  # 本地工具其实很快；保留真实 API 的行为形状
TOOL_RETRY_TIMES = 1  # 失败重试次数
SIMULATE_FAILURE_RATE = 0.5  # SIMULATE_TOOL_FAILURE=1 时抛错概率


# ---- 编排 ----

MAX_ADJUST_ROUNDS = 2  # 预算回环上限（文档 8.3）


# ---- 预算估算规则（文档 7.1 / 8.1 / 8.2）----

CITY_TRANSPORT_PER_DAY = 50  # 市内交通 元/天
BREAKFAST_PER_DAY = 15  # 早餐 元/天
INTERCITY_RATE_PER_KM = 0.45  # 高铁二等座 元/km
MISC_RATE = 0.10  # 机动预留比例
OVER_THRESHOLD = 1.05  # total > budget*1.05 判 over
UNDER_THRESHOLD = 0.5  # total < budget*0.5 判 under
LODGING_RATIO_THRESHOLD = 0.40  # 住宿占比超过则建议降档
REPLACE_PAID_MAX = 2  # 每轮最多替换的付费景点数


# ---- 酒店档位价位区间（文档 7.2），也是降档顺序依据 ----

HOTEL_TIER_PRICE_RANGE: dict[str, tuple[int, int]] = {
    "经济型": (150, 300),
    "舒适型": (300, 600),
    "高档型": (600, 1200),
}


# ---- 城际距离表（km，单程，粗估；文档 7.3）----
# 只列常用组合，缺失时按区域粗略规则估算。

CITY_DISTANCES_KM: dict[tuple[str, str], int] = {
    ("北京", "上海"): 1300,
    ("北京", "成都"): 2100,
    ("北京", "西安"): 1100,
    ("北京", "杭州"): 1300,
    ("北京", "重庆"): 1750,
    ("北京", "长沙"): 1500,
    ("北京", "青岛"): 700,
    ("北京", "南京"): 1000,
    ("北京", "广州"): 2100,
    ("上海", "成都"): 1900,
    ("上海", "西安"): 1400,
    ("上海", "杭州"): 180,
    ("上海", "重庆"): 1700,
    ("上海", "长沙"): 1100,
    ("上海", "青岛"): 700,
    ("上海", "南京"): 300,
    ("上海", "广州"): 1500,
    ("成都", "西安"): 700,
    ("成都", "杭州"): 1900,
    ("成都", "重庆"): 300,
    ("成都", "长沙"): 1200,
    ("成都", "青岛"): 1900,
    ("成都", "南京"): 1650,
    ("成都", "广州"): 1600,
    ("西安", "杭州"): 1350,
    ("西安", "重庆"): 700,
    ("西安", "长沙"): 980,
    ("西安", "青岛"): 1180,
    ("西安", "南京"): 1080,
    ("西安", "广州"): 1800,
    ("杭州", "重庆"): 1400,
    ("杭州", "长沙"): 1000,
    ("杭州", "青岛"): 850,
    ("杭州", "南京"): 280,
    ("杭州", "广州"): 1300,
    ("重庆", "长沙"): 900,
    ("重庆", "青岛"): 1750,
    ("重庆", "南京"): 1450,
    ("重庆", "广州"): 1350,
    ("长沙", "青岛"): 1500,
    ("长沙", "南京"): 900,
    ("长沙", "广州"): 650,
    ("青岛", "南京"): 680,
    ("青岛", "广州"): 2000,
    ("南京", "广州"): 1350,
}

# 区域划分：用于距离表缺失时的粗估（同区域 500km / 跨区域 1200km）
CITY_REGIONS: dict[str, str] = {
    "北京": "华北",
    "上海": "华东",
    "杭州": "华东",
    "南京": "华东",
    "青岛": "华东",
    "成都": "西南",
    "重庆": "西南",
    "西安": "西北",
    "长沙": "华中",
    "广州": "华南",
}

SAME_REGION_DISTANCE_KM = 500
CROSS_REGION_DISTANCE_KM = 1200


def intercity_distance_km(departure: str, destination: str) -> int:
    """查距离表；缺失按区域粗估。往返由调用方乘 2。"""
    key = (departure, destination)
    alt = (destination, departure)
    if key in CITY_DISTANCES_KM:
        return CITY_DISTANCES_KM[key]
    if alt in CITY_DISTANCES_KM:
        return CITY_DISTANCES_KM[alt]
    same = CITY_REGIONS.get(departure) == CITY_REGIONS.get(destination)
    return SAME_REGION_DISTANCE_KM if same else CROSS_REGION_DISTANCE_KM
