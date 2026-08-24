"""CLI 入口的退出行为测试（离线，不碰真实 LLM）。"""

from __future__ import annotations

import sys

import pytest

from travel_planner import main as main_mod


def test_ctrl_c_at_prompt_exits_cleanly(monkeypatch, capsys) -> None:
    """在"您的旅行需求"输入处按 Ctrl+C：干净退出（130），不抛 traceback。"""

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
