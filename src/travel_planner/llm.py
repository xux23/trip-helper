"""LLM 接入层：OpenAI 兼容协议（文档 10.1 节选型）。

通过 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 三个环境变量切换任意兼容服务商。
测试注入同形状的假客户端即可离线跑通全流程。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from . import config


@dataclass
class LLMResponse:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class UsageStats:
    """token 用量统计（评估报告用，NFR2 成本口径）。"""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def record(self, resp: LLMResponse) -> None:
        self.calls += 1
        self.prompt_tokens += resp.prompt_tokens
        self.completion_tokens += resp.completion_tokens


class LLMClient(Protocol):
    model: str
    usage: UsageStats

    async def chat(
        self, messages: list[dict[str, str]], *, temperature: float, max_tokens: int
    ) -> LLMResponse: ...


class OpenAICompatClient:
    """网络错误/超时自动重试 1 次（文档第 9 章）。SDK 自身重试关掉，统一在这里控制。"""

    def __init__(self) -> None:
        from openai import AsyncOpenAI

        self.model = config.LLM_MODEL
        self.usage = UsageStats()
        self._client = AsyncOpenAI(
            base_url=config.LLM_BASE_URL,
            api_key=config.LLM_API_KEY,
            max_retries=0,
            timeout=60,
        )

    async def chat(
        self, messages: list[dict[str, str]], *, temperature: float, max_tokens: int
    ) -> LLMResponse:
        import openai

        last_err: Exception | None = None
        for attempt in range(2):
            try:
                resp: Any = await self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                out = LLMResponse(
                    text=resp.choices[0].message.content or "",
                    prompt_tokens=getattr(resp.usage, "prompt_tokens", 0) or 0,
                    completion_tokens=getattr(resp.usage, "completion_tokens", 0) or 0,
                )
                self.usage.record(out)
                return out
            except (openai.APIConnectionError, openai.APITimeoutError, openai.InternalServerError) as e:
                last_err = e
        raise RuntimeError(f"LLM 调用失败（已重试 1 次）：{last_err}") from last_err


def make_llm_client() -> LLMClient | None:
    """未配置 API Key 时返回 None，由 CLI 提示用户。"""
    if not config.LLM_API_KEY:
        return None
    return OpenAICompatClient()
