"""对比评估脚本（文档第 11 章）：基线单 LLM 直出 vs 多 Agent 流程。

用法：
    uv run python tests/eval.py --limit 5          # 快速试跑
    uv run python tests/eval.py                    # 全量 50 条
    uv run python tests/eval.py --report           # 输出 docs/评估报告.md

需要配置 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from travel_planner.agents.planner import parse_json_text  # noqa: E402
from travel_planner.llm import make_llm_client  # noqa: E402
from travel_planner.orchestrator import SilentIO, plan  # noqa: E402
from travel_planner.schemas import Itinerary, TravelRequest, build_travel_request  # noqa: E402
from travel_planner.tools.weather import fallback_climate, make_weather  # noqa: E402

CASES_PATH = Path(__file__).parent / "eval_cases.json"

# 成本折算常量（元 / 百万 token）。不同服务商价格差异大，可用环境变量覆盖；
# 评估报告里同时披露原始 token 数，避免单一折算口径误导。
PRICE_IN_PER_M = float(os.environ.get("EVAL_PRICE_IN_PER_M", "1500"))
PRICE_OUT_PER_M = float(os.environ.get("EVAL_PRICE_OUT_PER_M", "6000"))

BASELINE_SYSTEM_PROMPT = """你是一名旅行规划师。请根据用户的旅行需求，直接输出一份行程 JSON。

要求：每天安排上午/下午两个景点和一个晚间安排；每天给出午餐和晚餐推荐；
全程推荐一家酒店；估算总费用。

只输出一个 JSON 对象，结构如下，不要输出任何解释或 markdown 标记：
{
  "destination": "城市名",
  "hotel": {"name": "...", "tier": "经济型/舒适型/高档型", "price_per_night": 0, "area": "...", "tags": []},
  "days": [
    {"day": 1, "date": null,
     "morning": {"attraction": {"name":"...","tags":[],"price":0,"duration_hours":0,"indoor":false,"rating":4.5,"area":"..."}},
     "afternoon": {"attraction": {...}},
     "evening": {"text": "..."},
     "meals": {"lunch": {"name":"...","price_per_person":0}, "dinner": {"name":"...","price_per_person":0}}}
  ],
  "estimated_total": 0,
  "breakdown": {"transport_intercity":0,"transport_city":0,"lodging":0,"food":0,"tickets":0,"misc":0}
}"""


def load_cases(limit: int | None) -> list[dict]:
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    return cases[:limit] if limit else cases


def synth_weather(request: TravelRequest) -> list[dict]:
    """评估打分用的逐日天气（通用气候兜底生成，确定性）。"""
    from travel_planner.agents.info import build_dates

    rows = []
    for i, d in enumerate(build_dates(request.date, request.days)):
        rows.append(make_weather(request.destination, d, i, fallback_climate()))
    return rows


def pools_from_results(results: list) -> dict[str, dict]:
    """从规划产出的 InfoResults 构建 名称→{tags, indoor} 参照表（打分用）。"""
    pools: dict[str, dict] = {}
    for r in results:
        for item in r.data:
            pools[item["name"]] = {
                "tags": item.get("tags", []),
                "indoor": item.get("indoor", True),
            }
    return pools


def score(
    itinerary_dict: dict | None,
    request: TravelRequest,
    weather: list[dict],
    pools: dict[str, dict] | None = None,
) -> list[str]:
    """文档 11.2 的 10 项 checklist，返回每项是否通过。全部可自动判定。

    pools 为 名称→{tags, indoor} 参照表（多 Agent 路径传规划候选池；基线无数据
    传 None，涉及偏好/室内的检查项按保守记 0）。
    """
    checks: list[bool] = []

    def fail_all_from(n: int):
        checks.extend([False] * n)

    if itinerary_dict is None:
        fail_all_from(10)
        return [str(b) for b in checks]

    days = itinerary_dict.get("days") or []
    hotel = itinerary_dict.get("hotel")

    # 1 每天景点数 1~4
    counts = []
    for d in days:
        n = sum(
            1
            for key in ("morning", "afternoon", "evening")
            if isinstance(d.get(key), dict) and d[key].get("attraction")
        )
        counts.append(n)
    checks.append(bool(days) and all(1 <= c <= 4 for c in counts))

    # 2 每天有午餐和晚餐
    ok_meals = bool(days)
    for d in days:
        meals = d.get("meals") or {}
        if not (isinstance(meals.get("lunch"), dict) and isinstance(meals.get("dinner"), dict)):
            ok_meals = False
    checks.append(ok_meals)

    # 3 全程恰好 1 家酒店
    checks.append(isinstance(hotel, dict) and bool(hotel.get("name")))

    # 4 住宿晚数 = days - 1（天数对齐 + 酒店存在）
    checks.append(isinstance(hotel, dict) and len(days) == request.days)

    # 5 费用偏差 ≤15%（over 且未解决记 0）
    total = itinerary_dict.get("estimated_total")
    if isinstance(total, (int, float)) and total > 0:
        dev = abs(total - request.budget) / request.budget
        checks.append(dev <= 0.15)
    else:
        checks.append(False)

    # 6 景点、餐厅无重复
    a_names = [
        d[k]["attraction"]["name"]
        for d in days
        for k in ("morning", "afternoon", "evening")
        if isinstance(d.get(k), dict) and d[k].get("attraction")
    ]
    r_names = []
    for d in days:
        meals = d.get("meals") or {}
        for m in ("lunch", "dinner"):
            if isinstance(meals.get(m), dict) and meals[m].get("name"):
                r_names.append(meals[m]["name"])
    checks.append(len(a_names) == len(set(a_names)) and len(r_names) == len(set(r_names)))

    # 7 偏好命中次数 ≥ days（无候选池记 0 分，保守）
    if request.preferences:
        pref_set = set(request.preferences)
        hits = 0
        if pools:
            hits = sum(
                1
                for name in a_names + r_names
                if pref_set & set(pools.get(name, {}).get("tags", []))
            )
        checks.append(hits >= request.days)
    else:
        checks.append(True)

    # 8 雨天不安排纯户外景点（无雨天记 1 分；无候选池且雨天记 0 分）
    rain_days = [w for w in weather if w.get("rain")]
    if not rain_days:
        checks.append(True)
    elif pools is None:
        checks.append(False)
    else:
        bad = any(
            pools.get(name, {}).get("indoor", True) is False
            for d in days
            for k in ("morning", "afternoon")
            if isinstance(d.get(k), dict) and (name := (d[k].get("attraction") or {}).get("name"))
        )
        checks.append(not bad)

    # 9 每天景点总时长 ≤8h
    durations_ok = bool(days)
    for d in days:
        hours = sum(
            float(d[k]["attraction"].get("duration_hours", 99))
            for k in ("morning", "afternoon", "evening")
            if isinstance(d.get(k), dict) and d[k].get("attraction")
        )
        if hours > 8:
            durations_ok = False
    checks.append(durations_ok)

    # 10 合法 JSON 且必填字段齐全
    required = ["destination", "hotel", "days"]
    checks.append(all(itinerary_dict.get(k) is not None for k in required) and bool(days))

    assert len(checks) == 10
    return [str(int(b)) for b in checks]


async def eval_agent_case(llm, case: dict) -> dict:
    start = time.monotonic()
    usage_before = llm.usage.calls, llm.usage.prompt_tokens, llm.usage.completion_tokens
    row = {"id": case["id"], "mode": "agent"}
    try:
        ctx = await plan(case["text"], llm, SilentIO())
        weather_dicts = [w.model_dump() for w in ctx.weather]
        pools = pools_from_results(ctx.results)
        scores = score(ctx.itinerary.model_dump(), ctx.request, weather_dicts, pools)
        row.update(
            elapsed=round(time.monotonic() - start, 2),
            status=ctx.report.status,
            estimated=ctx.report.estimated_total,
            budget=ctx.request.budget,
            degraded=ctx.itinerary.meta.degraded,
            rounds=ctx.itinerary.meta.adjust_rounds,
            scores=scores,
            reasonable=sum(int(s) for s in scores) >= 7,
        )
    except Exception as e:
        row.update(elapsed=round(time.monotonic() - start, 2), error=str(e)[:120],
                   scores=["0"] * 10, reasonable=False)
    calls_after = llm.usage.calls, llm.usage.prompt_tokens, llm.usage.completion_tokens
    row["llm_calls"] = calls_after[0] - usage_before[0]
    row["tokens"] = (calls_after[1] - usage_before[1]) + (calls_after[2] - usage_before[2])
    return row


async def eval_baseline_case(llm, case: dict) -> dict:
    start = time.monotonic()
    usage_before = llm.usage.calls, llm.usage.prompt_tokens, llm.usage.completion_tokens
    row = {"id": case["id"], "mode": "baseline"}
    messages = [
        {"role": "system", "content": BASELINE_SYSTEM_PROMPT.format(today=date.today().isoformat())},
        {"role": "user", "content": case["text"]},
    ]
    try:
        resp = await llm.chat(messages, temperature=0.3, max_tokens=4000)
        raw_request = await _extract_for_baseline(llm, case["text"])
        parsed: dict | None = None
        try:
            parsed = parse_json_text(resp.text)
        except Exception:
            parsed = None
        scores = score(parsed, raw_request, synth_weather(raw_request))
        row.update(
            elapsed=round(time.monotonic() - start, 2),
            estimated=(parsed or {}).get("estimated_total"),
            budget=raw_request.budget,
            scores=scores,
            reasonable=sum(int(s) for s in scores) >= 7,
        )
    except Exception as e:
        row.update(elapsed=round(time.monotonic() - start, 2), error=str(e)[:120],
                   scores=["0"] * 10, reasonable=False)
    calls_after = llm.usage.calls, llm.usage.prompt_tokens, llm.usage.completion_tokens
    row["llm_calls"] = calls_after[0] - usage_before[0]
    row["tokens"] = (calls_after[1] - usage_before[1]) + (calls_after[2] - usage_before[2])
    return row


async def _extract_for_baseline(llm, text: str) -> TravelRequest:
    """基线也需要结构化需求来打分：复用 Planner 的抽取 Prompt。"""
    from travel_planner.agents.planner import EXTRACT_SYSTEM_PROMPT, ExtractedRequest

    resp = await llm.chat(
        [
            {"role": "system", "content": EXTRACT_SYSTEM_PROMPT.format(today=date.today().isoformat())},
            {"role": "user", "content": text},
        ],
        temperature=0,
        max_tokens=500,
    )
    try:
        raw = ExtractedRequest.model_validate(parse_json_text(resp.text))
    except Exception:
        return TravelRequest(destination="未知", days=3, budget=3000)
    req, _ = build_travel_request(raw)
    return req


def summarize(rows: list[dict], mode: str) -> dict:
    sub = [r for r in rows if r["mode"] == mode]
    n = len(sub) or 1
    return {
        "n": len(sub),
        "reasonable_rate": round(sum(1 for r in sub if r.get("reasonable")) / n, 3),
        "avg_elapsed": round(sum(r.get("elapsed", 0) for r in sub) / n, 2),
        "avg_llm_calls": round(sum(r.get("llm_calls", 0) for r in sub) / n, 2),
        "total_tokens": sum(r.get("tokens", 0) for r in sub),
        "errors": sum(1 for r in sub if r.get("error")),
    }


def render_report(cases: list[dict], agent_rows: list[dict], base_rows: list[dict]) -> str:
    sa, sb = summarize(agent_rows, "agent"), summarize(base_rows, "baseline")
    cost_a = (
        sa["total_tokens"] / 1e6 * PRICE_IN_PER_M * 0  # 占位：按统一混合价近似
    )
    lines = [
        "# 评估报告",
        "",
        f"> 生成时间：{date.today().isoformat()}　用例数：{sa['n']}　"
        f"成本口径：输入 {PRICE_IN_PER_M} 元/M token，输出 {PRICE_OUT_PER_M} 元/M token",
        "",
        "## 汇总对比",
        "",
        "| 指标 | 基线（单 LLM 直出） | 多 Agent | 目标 |",
        "|---|---|---|---|",
        f"| 合理率（≥7/10 分） | {sb['reasonable_rate']:.0%} | {sa['reasonable_rate']:.0%} | ≥85% |",
        f"| 平均耗时（秒） | {sb['avg_elapsed']} | {sa['avg_elapsed']} | ≤15s |",
        f"| 平均 LLM 调用次数 | {sb['avg_llm_calls']} | {sa['avg_llm_calls']} | 2~4 次 |",
        f"| 总 token 用量 | {sb['total_tokens']} | {sa['total_tokens']} | — |",
        f"| 折算成本（元） | {sb['total_tokens']/1e6*PRICE_IN_PER_M:.3f} | "
        f"{(cost_a := sa['total_tokens']/1e6*PRICE_IN_PER_M):.3f} | ≤0.15 元/次 |",
        f"| 失败用例数 | {sb['errors']} | {sa['errors']} | — |",
        "",
        "## 明细（多 Agent）",
        "",
        "| id | 耗时s | 状态 | 估算/预算 | 分项得分 | 合理 | 备注 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in agent_rows:
        lines.append(
            f"| {r['id']} | {r.get('elapsed','-')} | {r.get('status','-')} "
            f"| {r.get('estimated','-')}/{r.get('budget','-')} | {''.join(r.get('scores', []))} "
            f"| {'Y' if r.get('reasonable') else 'N'} | {(r.get('error') or '')[:60]} |"
        )
    lines += ["", "## 明细（基线）", "", "| id | 耗时s | 估算/预算 | 分项得分 | 合理 | 备注 |",
              "|---|---|---|---|---|---|"]
    for r in base_rows:
        lines.append(
            f"| {r['id']} | {r.get('elapsed','-')} | {r.get('estimated','-')}/{r.get('budget','-')} "
            f"| {''.join(r.get('scores', []))} | {'Y' if r.get('reasonable') else 'N'} "
            f"| {(r.get('error') or '')[:60]} |"
        )
    lines += ["", "## 失败案例分析", ""]
    failures = [r for r in agent_rows if not r.get("reasonable")]
    if not failures:
        lines.append("本轮无失败用例。")
    for r in failures:
        detail = ", ".join(
            name
            for name, ok in zip(
                ["景点数", "午晚餐", "唯一酒店", "晚数", "费用偏差", "无重复", "偏好命中",
                 "雨天室内", "时长≤8h", "字段完整"],
                r.get("scores", []),
            )
            if ok == "0"
        )
        lines.append(f"- {r['id']}: 未通过项 → {detail or r.get('error', '未知')}")
    return "\n".join(lines) + "\n"


async def main_async(args: argparse.Namespace) -> int:
    llm = make_llm_client()
    if llm is None:
        print("未配置 LLM 环境变量（LLM_BASE_URL/LLM_API_KEY/LLM_MODEL），无法评估")
        return 1
    cases = load_cases(args.limit)
    print(f"共 {len(cases)} 条用例；先跑多 Agent，再跑基线……")

    agent_rows = []
    for i, case in enumerate(cases, 1):
        row = await eval_agent_case(llm, case)
        mark = "Y" if row.get("reasonable") else "N"
        print(f"[agent {i:>2}/{len(cases)}] {row['id']} 合理={mark} 耗时={row['elapsed']}s")
        agent_rows.append(row)

    base_rows = []
    for i, case in enumerate(cases, 1):
        row = await eval_baseline_case(llm, case)
        mark = "Y" if row.get("reasonable") else "N"
        print(f"[base  {i:>2}/{len(cases)}] {row['id']} 合理={mark} 耗时={row['elapsed']}s")
        base_rows.append(row)

    print("\n=== 汇总 ===")
    print("多Agent:", summarize(agent_rows, "agent"))
    print("基线:   ", summarize(base_rows, "baseline"))

    report = render_report(cases, agent_rows, base_rows)
    out_path = Path(__file__).resolve().parents[1] / "docs" / "评估报告.md"
    out_path.write_text(report, encoding="utf-8")
    print(f"报告已写入 {out_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="50 用例对比评估")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 条")
    parser.add_argument("--report", action="store_true", help="写入 docs/评估报告.md（默认写）")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
