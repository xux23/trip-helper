"""离线数据加载 + 故障注入（文档第 6 章）。

城市无数据文件 → 抛 CityNotFoundError，由 Info Agent 捕获后走 fallback。
SIMULATE_TOOL_FAILURE=1 时工具层以 50% 概率抛错，用于演示兜底逻辑。
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any, TypedDict

from travel_planner import config

DATA_DIR = Path(__file__).parent / "data"


class ToolResult(TypedDict):
    """统一工具返回形状（文档 6.2 节）：来源标记 + 数据列表。"""

    source: str  # "local" | "fallback"
    data: list[dict[str, Any]]

_city_cache: dict[str, dict[str, Any]] = {}


class CityNotFoundError(Exception):
    """城市没有离线数据文件。"""


def normalize_city(city: str) -> str:
    """"成都市"→"成都"，容忍用户/模型带行政后缀。"""
    return city.strip().removesuffix("市").strip()


def load_city(city: str) -> dict[str, Any]:
    key = city.strip()
    if key in _city_cache:
        return _city_cache[key]
    path = DATA_DIR / "cities" / f"{key}.json"
    if not path.exists():
        key = normalize_city(city)
        path = DATA_DIR / "cities" / f"{key}.json"
    if not path.exists():
        raise CityNotFoundError(f"暂不支持城市：{city}")
    data = json.loads(path.read_text(encoding="utf-8"))
    _city_cache[key] = data
    return data


def load_fallback() -> dict[str, Any]:
    return json.loads((DATA_DIR / "fallback.json").read_text(encoding="utf-8"))


def maybe_fail(tool_name: str) -> None:
    """故障注入：仅当环境开关打开时，以固定概率抛错。"""
    if config.SIMULATE_TOOL_FAILURE and random.random() < config.SIMULATE_FAILURE_RATE:
        raise OSError(f"[故障注入] {tool_name} 调用失败")
