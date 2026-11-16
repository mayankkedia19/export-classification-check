"""Per-run execution context: budgets, deadline, evidence store, step recording, guarded LLM access."""
from __future__ import annotations

import json
import logging
import time
import uuid

from opentelemetry import trace

from app.config import get_settings
from app.corpus.store import CorpusStore
from app.db import Step, session_scope
from app.guardrails import GuardrailViolation, assert_safe_for_model, neutralise
from app.llm.base import LLMFormatError, ProviderUnavailable
from app.llm.resilience import ResilientLLM
from app.logging_setup import log
from app.metrics import AGENT_TASKS, COST, GUARDRAIL_HITS, STEP_LATENCY, TOKENS
from app.prompts import PromptSet, load_prompts

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("ecc.agents")


class RunTimeout(Exception):
    pass


class StepLimitExceeded(Exception):
    pass


class ToolBudgetExceeded(Exception):
    pass


def parse_json_object(text: str) -> dict:
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        raise LLMFormatError("no JSON object in model output")
    try:
        d = json.loads(text[a : b + 1])
    except json.JSONDecodeError as e:
        raise LLMFormatError(f"invalid JSON: {e}") from e
    if not isinstance(d, dict):
        raise LLMFormatError("JSON output is not an object")
    return d


class RunContext:
    def __init__(self, run_id: str, request: dict, store: CorpusStore, llm: ResilientLLM,
                 prompt_version: str | None = None, persist: bool = True, started_monotonic: float | None = None):
        s = get_settings()
        self.s = s
        self.run_id = run_id
        self.request = request
        self.store = store
        self.llm = llm
        self.prompts: PromptSet = load_prompts(prompt_version or s.prompt_version)
        self.persist = persist
        self.deadline = time.time() + s.run_timeout_s
        self.retrieved: dict[str, dict] = {}
        self.seq = 0
        self.tool_calls = 0
        self.llm_calls = 0
        self.retries = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cost = 0.0
        self.by_step: dict[str, dict[str, float]] = {}
        self.steps_log: list[dict] = []
        self.loop_steps: dict[str, int] = {}
        self.entities: dict = {}

    # ---- budgets ----
    def remaining(self) -> float:
        return self.deadline - time.time()

    def check_deadline(self) -> None:
        if self.remaining() <= 0.3:
            raise RunTimeout("run wall-clock budget exhausted")

    def bump_loop(self, agent: str) -> None:
        self.loop_steps[agent] = self.loop_steps.get(agent, 0) + 1
        if self.loop_steps[agent] > self.s.max_agent_steps * max(1, self.s.max_research_attempts):
            raise StepLimitExceeded(f"{agent}: step limit exceeded")

    # ---- evidence ----
    def add_passages(self, passages: list[dict]) -> None:
        for p in passages:
            self.retrieved.setdefault(p["passage_id"], {k: v for k, v in p.items() if k not in ("embed_text",)})

    def request_fields(self) -> dict:
        r = self.request
        keep = ("product_description", "materials", "intended_use", "capacity_litres", "weight_grams", "quantity", "export_intent")
        return {k: (neutralise(v) if isinstance(v, str) else ([neutralise(x, 60) for x in v] if isinstance(v, list) else v))
                for k, v in r.items() if k in keep and v not in (None, [])}

    # ---- recording ----
    def record(self, agent: str, kind: str, name: str, inp=None, out=None, latency_ms: float = 0.0, pt: int = 0, ct: int = 0,
               cost: float = 0.0, status: str = "ok", error: str | None = None) -> None:
        self.seq += 1
        key = f"{agent}.{name}"
        d = self.by_step.setdefault(key, {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0, "calls": 0, "latency_ms": 0.0})
        d["prompt_tokens"] += pt
        d["completion_tokens"] += ct
        d["cost_usd"] += cost
        d["calls"] += 1
        d["latency_ms"] += latency_ms
        STEP_LATENCY.labels(agent, kind, name).observe(latency_ms / 1000)
        if pt or ct:
            TOKENS.labels("prompt", agent, name).inc(pt)
            TOKENS.labels("completion", agent, name).inc(ct)
            COST.labels(agent, name).inc(cost)
        sp = trace.get_current_span()
        sp.add_event(f"{kind}:{name}", {"agent": agent, "latency_ms": latency_ms, "status": status})
        rec = dict(seq=self.seq, agent=agent, kind=kind, name=name, input=inp, output=out, latency_ms=round(latency_ms, 2),
                   prompt_tokens=pt, completion_tokens=ct, cost_usd=cost, prompt_version=self.prompts.version, status=status, error=error)
        self.steps_log.append(rec)
        if self.persist:
            try:
                with session_scope() as s:
                    s.add(Step(run_id=self.run_id, seq=self.seq, agent=agent, kind=kind, name=name, input_json=_jsonable(inp),
                               output_json=_jsonable(out), latency_ms=rec["latency_ms"], prompt_tokens=pt, completion_tokens=ct,
                               cost_usd=cost, prompt_version=self.prompts.version, status=status, error=error))
            except Exception as e:  # recording must never kill the run
                log(logger, logging.ERROR, "step persistence failed", err=type(e).__name__)

    # ---- guarded LLM access ----
    def llm_json(self, agent: str, role: str, step_name: str, state: dict) -> dict:
        self.check_deadline()
        req = self.request_fields()
        untrusted = json.dumps(req, ensure_ascii=False)
        state_s = json.dumps(state, ensure_ascii=False, default=str)
        try:
            assert_safe_for_model(untrusted, f"{agent}.{step_name}")
        except GuardrailViolation:
            GUARDRAIL_HITS.labels("injection_at_model_boundary").inc()
            self.record(agent, "system", "guardrail_block", {"step": step_name}, {"blocked": True}, status="blocked")
            raise
        user = f"<untrusted_input>{untrusted}</untrusted_input>\n<state>{state_s}</state>"
        messages = [{"role": "system", "content": self.prompts.texts[role]}, {"role": "user", "content": user}]
        t0 = time.time()
        with tracer.start_as_current_span(f"llm.{agent}.{step_name}") as sp:
            sp.set_attribute("prompt.version", self.prompts.version)
            try:
                r = self.llm.call(messages, self.s.llm_timeout_s, self.deadline)
            except ProviderUnavailable as e:
                self.record(agent, "llm", step_name, {"role": role}, None, (time.time() - t0) * 1000, status="error", error=str(e))
                raise
            cost = r.prompt_tokens / 1000 * self.s.price_prompt_per_1k + r.completion_tokens / 1000 * self.s.price_completion_per_1k
            self.llm_calls += 1
            self.retries += r.attempts - 1
            self.prompt_tokens += r.prompt_tokens
            self.completion_tokens += r.completion_tokens
            self.cost += cost
            lat = (time.time() - t0) * 1000
            try:
                out = parse_json_object(r.text)
            except LLMFormatError as e:
                self.record(agent, "llm", step_name, {"role": role}, {"raw": r.text[:200]}, lat, r.prompt_tokens, r.completion_tokens, cost, "error", str(e))
                raise
            self.record(agent, "llm", step_name, {"role": role, "state_keys": sorted(state)}, out, lat, r.prompt_tokens, r.completion_tokens, cost)
            return out


def _jsonable(x):
    if x is None:
        return None
    try:
        return json.loads(json.dumps(x, default=str))
    except Exception:
        return {"repr": str(x)[:500]}


def new_run_id() -> str:
    return uuid.uuid4().hex[:20]
