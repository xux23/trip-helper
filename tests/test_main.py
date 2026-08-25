"""CLI 入口的退出行为测试（离线，不碰真实 LLM）。"""

from __future__ import annotations

import sys

import pytest

from travel_planner import main as main_mod


def test_ctrl_c_at_prompt_exits_cleanly(monkeypatch, capsys) -> None:
    """在输入询问处按 Ctrl+C：干净退出（130），不抛 traceback。"""

    def ask_raises(self, prompt: str) -> str:
        raise KeyboardInterrupt()

    monkeypatch.setattr(main_mod.CLI, "ask", ask_raises)
    monkeypatch.setattr(main_mod, "make_llm_client", lambda: object())
    monkeypatch.setattr(sys, "argv", ["main.py"])  # 无命令行参数，确保走到输入环节

    with pytest.raises(SystemExit) as exc:
        main_mod.cli()

    assert exc.value.code == 130
    out = capsys.readouterr().out
    assert "已取消" in out


def test_thinking_animation_lifecycle(capsys) -> None:
    """思考动画：启动后产生帧输出（含文案与耗时），停止后返回动画时长。"""
    import asyncio

    async def run():
        main_mod._start_thinking("测试阶段")
        await asyncio.sleep(0.15)
        return main_mod._stop_thinking()

    elapsed = asyncio.run(run())
    out = capsys.readouterr().out
    assert elapsed >= 0.1
    assert "测试阶段" in out


def test_render_budget_unfeasible() -> None:
    """预算不现实提示：含最低可行预算、折算每天、同类比价与建议档位。"""
    from travel_planner.schemas import BREAKDOWN_KEYS, BudgetReport

    report = BudgetReport(
        status="over", budget=800, estimated_total=1317,
        breakdown={k: 0 for k in BREAKDOWN_KEYS}, suggestions=[], note=None,
    )
    out = main_mod.render_budget_unfeasible(report, "重庆", days=3)
    assert "不现实" in out
    assert "1317" in out
    assert "439" in out  # 每天约 1317/3 ≈ 439 元
    assert "奶茶" in out  # 同类比价（普通人参照物）
    assert "1400" in out  # 1317×1.1 ≈ 1449 → 取整到百 = 1400（建议预算）
    assert "重庆" in out
