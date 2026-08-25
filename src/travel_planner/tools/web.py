"""免费网页搜索工具（文档第 6 章）：给 Agent 不确定时补充网上的实用信息。

数据源（均免费、无需 Key）：
- `bing`：必应中国搜索页（cn.bing.com/search）——国内可直连、返回真实网页结果，默认首选；
- `wikipedia`：维基百科中文搜索 API（zh.wikipedia.org）——国内通常无法访问，需网络允许才启用；
- `duckduckgo`：DuckDuckGo Instant Answer API（api.duckduckgo.com）——同上。

源顺序由 `WEB_SEARCH_SOURCES` 环境变量控制（默认 "bing"，可配
"bing,wikipedia,duckduckgo"）。任一只读源失败/无结果 → 自动降级到下一个或
返回空（source=fallback），绝不中断主流程。
"""

from __future__ import annotations

import html as html_mod
import re
from typing import Any
from urllib.parse import quote

from travel_planner import config
from travel_planner.tools.loader import ToolResult, maybe_fail

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


async def _bing(keyword: str, limit: int) -> list[dict[str, str]]:
    """必应中国搜索页：解析 b_algo 结果块（title / snippet / url）。"""
    import httpx

    headers = {"User-Agent": _USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"}
    async with httpx.AsyncClient(timeout=6.0, headers=headers) as client:
        resp = await client.get("https://cn.bing.com/search", params={"q": keyword})
        resp.raise_for_status()
        page = resp.text
    items: list[dict[str, str]] = []
    for block in re.findall(r'<li class="b_algo".*?</li>', page, re.S)[:limit]:
        m = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        url, title = m.group(1), _strip_html(m.group(2))
        snip = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        snippet = _strip_html(snip.group(1)) if snip else ""
        if not title:
            continue
        items.append(
            {
                "title": html_mod.unescape(title)[:40],
                "snippet": html_mod.unescape(snippet)[:100],
                "url": url,
            }
        )
    return items


async def _wikipedia_zh(keyword: str, limit: int) -> list[dict[str, str]]:
    """维基百科中文全文搜索：返回 title / snippet / url。"""
    import httpx

    params = {
        "action": "query",
        "list": "search",
        "srsearch": keyword,
        "srlimit": str(limit),
        "format": "json",
        "utf8": "1",
    }
    async with httpx.AsyncClient(timeout=6.0) as client:
        resp = await client.get("https://zh.wikipedia.org/w/api.php", params=params)
        resp.raise_for_status()
        hits = ((resp.json().get("query") or {}).get("search")) or []
    items = []
    for h in hits:
        title = str(h.get("title") or "").strip()
        if not title:
            continue
        items.append(
            {
                "title": title,
                "snippet": _strip_html(str(h.get("snippet") or ""))[:100],
                "url": f"https://zh.wikipedia.org/wiki/{quote(title)}",
            }
        )
    return items


async def _duckduckgo(keyword: str, limit: int) -> list[dict[str, str]]:
    """DuckDuckGo Instant Answer：抽象摘要 + 相关话题。"""
    import httpx

    params = {"q": keyword, "format": "json", "no_html": "1", "skip_disambig": "1"}
    async with httpx.AsyncClient(timeout=6.0) as client:
        resp = await client.get("https://api.duckduckgo.com/", params=params)
        resp.raise_for_status()
        data = resp.json()
    items: list[dict[str, str]] = []
    abstract = str(data.get("AbstractText") or "").strip()
    if abstract:
        items.append(
            {
                "title": str(data.get("Heading") or keyword)[:40],
                "snippet": abstract[:100],
                "url": str(data.get("AbstractURL") or ""),
            }
        )
    for t in (data.get("RelatedTopics") or [])[:limit]:
        if isinstance(t, dict) and t.get("Text"):
            items.append(
                {
                    "title": str(t.get("Text") or "")[:40],
                    "snippet": str(t.get("Text") or "")[:100],
                    "url": str(t.get("FirstURL") or ""),
                }
            )
    return items


_SOURCE_FUNC_NAMES = ("bing", "wikipedia", "duckduckgo")


async def search_web(keyword: str, limit: int = 3) -> ToolResult:
    """免费网页搜索：按 WEB_SEARCH_SOURCES 顺序尝试，都失败返回空（不中断流程）。"""
    maybe_fail("search_web")
    order = [n.strip() for n in config.WEB_SEARCH_SOURCES if n.strip() in _SOURCE_FUNC_NAMES]
    order = order or ["bing"]
    for name in order:
        # 调用时动态取函数：便于测试注入，也支持运行时切换源
        func = {"bing": _bing, "wikipedia": _wikipedia_zh, "duckduckgo": _duckduckgo}[name]
        try:
            items = await func(keyword, limit)
        except Exception:
            items = []
        if items:
            return ToolResult(source="online", data=items[:limit])
    return ToolResult(source="fallback", data=[])


def search_web_sync(keyword: str, limit: int = 3) -> ToolResult:
    """同步入口（供调试/脚本用）。"""
    import asyncio

    return asyncio.run(search_web(keyword, limit))
