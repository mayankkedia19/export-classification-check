"""Timeout, bounded retry with exponential backoff, circuit breaker (A5)."""
from __future__ import annotations

import logging
import random
import threading
import time

from app.config import get_settings
from app.llm.base import LLMResponse, Provider, ProviderError, ProviderUnavailable
from app.logging_setup import log
from app.metrics import BREAKER_OPENED, BREAKER_STATE, LLM_CALLS, LLM_RETRIES

logger = logging.getLogger(__name__)


class CircuitBreaker:
    CLOSED, HALF_OPEN, OPEN = 0, 1, 2

    def __init__(self, threshold: int, reset_s: float):
        self.threshold, self.reset_s = threshold, reset_s
        self.failures = 0
        self.state = self.CLOSED
        self.opened_at = 0.0
        self.lock = threading.Lock()

    def allow(self) -> bool:
        with self.lock:
            if self.state == self.OPEN:
                if time.time() - self.opened_at >= self.reset_s:
                    self.state = self.HALF_OPEN
                    BREAKER_STATE.set(self.HALF_OPEN)
                    return True  # one probe
                return False
            return True

    def success(self) -> None:
        with self.lock:
            self.failures = 0
            if self.state != self.CLOSED:
                log(logger, logging.INFO, "circuit breaker closed")
            self.state = self.CLOSED
            BREAKER_STATE.set(0)

    def failure(self) -> None:
        with self.lock:
            self.failures += 1
            if self.state == self.HALF_OPEN or self.failures >= self.threshold:
                if self.state != self.OPEN:
                    BREAKER_OPENED.inc()
                    log(logger, logging.ERROR, "circuit breaker OPEN", failures=self.failures)
                self.state = self.OPEN
                self.opened_at = time.time()
                BREAKER_STATE.set(self.OPEN)

    def snapshot(self) -> dict:
        return {"state": ["closed", "half-open", "open"][self.state], "consecutive_failures": self.failures}


class ResilientLLM:
    def __init__(self, provider: Provider):
        s = get_settings()
        self.provider = provider
        self.breaker = CircuitBreaker(s.breaker_failure_threshold, s.breaker_reset_s)

    def call(self, messages: list[dict], timeout_s: float, deadline: float | None = None) -> LLMResponse:
        s = get_settings()
        if not self.breaker.allow():
            LLM_CALLS.labels("breaker_open").inc()
            raise ProviderUnavailable("circuit breaker open")
        last = None
        for attempt in range(s.llm_max_retries + 1):
            remaining = (deadline - time.time()) if deadline else timeout_s
            if remaining <= 0.2:
                raise ProviderUnavailable("run deadline reached before LLM call")
            try:
                r = self.provider.chat(messages, min(timeout_s, remaining))
                self.breaker.success()
                LLM_CALLS.labels("ok").inc()
                r.attempts = attempt + 1
                return r
            except ProviderError as e:
                last = e
                LLM_CALLS.labels("error").inc()
                if str(e).endswith("non_retryable"):
                    break
                if attempt < s.llm_max_retries:
                    LLM_RETRIES.inc()
                    delay = s.llm_backoff_base_s * (2 ** attempt) * (0.75 + random.random() / 2)
                    time.sleep(min(delay, max(0.0, (deadline - time.time()) - 0.3) if deadline else delay))
        self.breaker.failure()
        raise ProviderUnavailable(f"provider failed after retries: {last}")


_llm: ResilientLLM | None = None
_lock = threading.Lock()


def get_llm() -> ResilientLLM:
    global _llm
    with _lock:
        if _llm is None:
            from app.llm.providers import build_provider

            _llm = ResilientLLM(build_provider())
        return _llm


def reset_llm() -> None:
    global _llm
    _llm = None
