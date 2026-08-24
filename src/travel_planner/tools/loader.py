"""通用兜底数据加载 + 故障注入（文档第 6 章）。

离线 10 城数据已移除，数据源为在线高德 API（tools/amap.py）。
本模块只负责两件事：
- load_fallback：在线查询失败时使用的通用兜底（非城市特异性）；
- maybe_fail：SIMULATE_TOOL_FAILURE=1 时以固定概率抛错，演示降级。
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

    source: str  # "online" | "fallback"
    data: list[dict[str, Any]]


def load_fallback() -> dict[str, Any]:
    return json.loads((DATA_DIR / "fallback.json").read_text(encoding="utf-8"))


def maybe_fail(tool_name: str) -> None:
    """故障注入：仅当环境开关打开时，以固定概率抛错。"""
    if config.SIMULATE_TOOL_FAILURE and random.random() < config.SIMULATE_FAILURE_RATE:
        raise OSError(f"[故障注入] {tool_name} 调用失败")
