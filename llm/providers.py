"""Provider abstraction: swap provider by configuration only (spec §12)."""
from __future__ import annotations

import time

import httpx

from app.cache import cache_get_json
from app.config import get_settings
from app.llm.base import LLMResponse, Provider, ProviderError


class OpenAICompatibleProvider(Provider):
    """Any OpenAI-compatible /chat/completions endpoint: hosted free tiers, a local runtime, or the bundled stub service."""

    name = "openai_compatible"

    def __init__(self):
        s = get_settings()
        if not s.llm_base_url:
            raise RuntimeError("LLM_BASE_URL is required for the openai_compatible provider")
        self.base = s.llm_base_url.rstrip("/")
        self.model = s.llm_model
        self.key = s.llm_api_key
        self.client = httpx.Client()

    def chat(self, messages, timeout_s):
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        body = {"model": self.model, "messages": messages, "temperature": 0, "response_format": {"type": "json_object"}}
        t0 = time.time()
        try:
            r = self.client.post(f"{self.base}/chat/completions", json=body, headers=headers, timeout=timeout_s)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise ProviderError(f"{type(e).__name__}") from e
        if r.status_code >= 500 or r.status_code == 429:
            raise ProviderError(f"http_{r.status_code}")
        if r.status_code >= 400:
            raise ProviderError(f"http_{r.status_code}_non_retryable")
        d = r.json()
        u = d.get("usage") or {}
        txt = d["choices"][0]["message"]["content"]
        return LLMResponse(txt, int(u.get("prompt_tokens", len(str(messages)) // 4)),
                           int(u.get("completion_tokens", len(txt) // 4)), (time.time() - t0) * 1000)


class InProcessStubProvider(Provider):
    """Deterministic stub policy running in-process (unit tests, offline demos)."""

    name = "stub_inprocess"

    def chat(self, messages, timeout_s):
        from app.stubllm.policy import respond

        fault = (cache_get_json("chaos:llm") or {}).get("mode")
        if fault == "provider_down":
            raise ProviderError("stub_provider_down")
        t0 = time.time()
        text, pt, ct = respond(messages, consume_fault=True)
        if fault == "slow":
            time.sleep(min(timeout_s + 1, 3.0))
        return LLMResponse(text, pt, ct, (time.time() - t0) * 1000)


def build_provider() -> Provider:
    p = get_settings().llm_provider
    if p == "stub_inprocess":
        return InProcessStubProvider()
    if p == "openai_compatible":
        return OpenAICompatibleProvider()
    raise RuntimeError(f"unknown LLM_PROVIDER {p}")
