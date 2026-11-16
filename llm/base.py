from __future__ import annotations

from dataclasses import dataclass


class ProviderError(Exception):
    """Transient provider failure (timeout, 5xx, connection)."""


class ProviderUnavailable(Exception):
    """Circuit breaker is open, or retries are exhausted."""


class LLMFormatError(Exception):
    """Model returned something that is not the required JSON action."""


@dataclass
class LLMResponse:
    text: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float = 0.0
    attempts: int = 1


class Provider:
    name = "base"

    def chat(self, messages: list[dict], timeout_s: float) -> LLMResponse:  # pragma: no cover - interface
        raise NotImplementedError
