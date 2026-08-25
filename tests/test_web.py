"""免费网页搜索工具单测：源顺序（WEB_SEARCH_SOURCES）、逐级兜底、失败降级。"""

from __future__ import annotations

import asyncio

from travel_planner import config
from travel_planner.tools import web


def run(coro):
    return asyncio.run(coro)


def test_default_source_is_bing_only():
    """国内环境默认只走必应中国（cn.bing.com 可直连，维基/DDG 不可达）。"""
    assert config.WEB_SEARCH_SOURCES == ["bing"]


def test_search_web_prefers_first_configured_source(monkeypatch):
    monkeypatch.setattr(config, "WEB_SEARCH_SOURCES", ["bing", "wikipedia"])

    async def fake_bing(kw, limit):
        return [{"title": "宽窄巷子攻略", "snippet": "成都著名古街区", "url": "https://cn.bing.com/..."}]

    async def fake_wiki(kw, limit):
        raise AssertionError("首个源已命中，不应走到维基")

    monkeypatch.setattr(web, "_bing", fake_bing)
    monkeypatch.setattr(web, "_wikipedia_zh", fake_wiki)
    out = run(web.search_web("宽窄巷子"))
    assert out["source"] == "online"
    assert out["data"][0]["title"] == "宽窄巷子攻略"


def test_search_web_falls_back_to_wikipedia(monkeypatch):
    monkeypatch.setattr(config, "WEB_SEARCH_SOURCES", ["bing", "wikipedia", "duckduckgo"])

    async def fake_bing(kw, limit):
        return []

    async def fake_wiki(kw, limit):
        return [{"title": "宽窄巷子", "snippet": "维基内容", "url": "https://zh.wikipedia.org/wiki/宽窄巷子"}]

    monkeypatch.setattr(web, "_bing", fake_bing)
    monkeypatch.setattr(web, "_wikipedia_zh", fake_wiki)
    out = run(web.search_web("宽窄巷子"))
    assert out["data"][0]["title"] == "宽窄巷子"


def test_search_web_falls_back_to_ddg(monkeypatch):
    monkeypatch.setattr(config, "WEB_SEARCH_SOURCES", ["bing", "wikipedia", "duckduckgo"])

    async def empty(kw, limit):
        return []

    async def fake_ddg(kw, limit):
        return [{"title": "DDG 结果", "snippet": "s", "url": "u"}]

    monkeypatch.setattr(web, "_bing", empty)
    monkeypatch.setattr(web, "_wikipedia_zh", empty)
    monkeypatch.setattr(web, "_duckduckgo", fake_ddg)
    out = run(web.search_web("x"))
    assert out["data"][0]["title"] == "DDG 结果"


def test_search_web_empty_on_total_failure(monkeypatch):
    monkeypatch.setattr(config, "WEB_SEARCH_SOURCES", ["bing", "wikipedia", "duckduckgo"])

    async def broken(kw, limit):
        raise OSError("网络不可用")

    monkeypatch.setattr(web, "_bing", broken)
    monkeypatch.setattr(web, "_wikipedia_zh", broken)
    monkeypatch.setattr(web, "_duckduckgo", broken)
    out = run(web.search_web("x"))
    assert out["source"] == "fallback"
    assert out["data"] == []


def test_strip_html():
    assert web._strip_html('<span class="searchmatch">成都</span>博物馆') == "成都博物馆"
