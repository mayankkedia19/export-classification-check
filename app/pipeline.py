"""Run lifecycle: queued -> running -> completed | failed, with persistence, metrics and caching."""
from __future__ import annotations

import hashlib
import json
import logging
import time

from opentelemetry import trace
from opentelemetry.propagate import extract

from app.agents.context import RunContext
from app.agents.graph import run_graph
from app.cache import cache_get_json, cache_set_json
from app.config import get_settings
from app.corpus.store import get_store
from app.db import Run, Step, session_scope, utcnow
from app.llm.resilience import get_llm
from app.logging_setup import correlation_id_var, log, run_id_var
from app.metrics import RUN_DURATION, RUN_EXEC, RUNS_TOTAL, AGENT_TASKS
from app.tracing import current_trace_id

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("ecc.pipeline")


def cache_key(request: dict, prompt_version: str, corpus_version: str) -> str:
    norm = {k: (" ".join(v.lower().split()) if isinstance(v, str) else v) for k, v in sorted(request.items()) if v not in (None, [], "")}
    h = hashlib.sha256(json.dumps([norm, prompt_version, corpus_version], sort_keys=True, default=str).encode()).hexdigest()[:32]
    return f"runcache:{h}"


def _usage(ctx: RunContext) -> dict:
    return {"prompt_tokens": ctx.prompt_tokens, "completion_tokens": ctx.completion_tokens, "cost_usd": round(ctx.cost, 6),
            "llm_calls": ctx.llm_calls, "tool_calls": ctx.tool_calls, "steps": ctx.seq, "retries": ctx.retries,
            "by_step": {k: {kk: round(vv, 6) for kk, vv in v.items()} for k, v in ctx.by_step.items()}}


def execute_run(run_id: str, correlation_id: str | None = None, carrier: dict | None = None, persist_cache: bool = True) -> dict | None:
    s = get_settings()
    correlation_id_var.set(correlation_id or "-")
    run_id_var.set(run_id)
    parent = extract(carrier or {})
    with tracer.start_as_current_span("run.execute", context=parent) as span:
        span.set_attribute("run.id", run_id)
        span.set_attribute("correlation.id", correlation_id or "-")
        with session_scope() as db:
            run = db.get(Run, run_id)
            stale = bool(run and run.status == "running" and run.started_at
                         and (utcnow() - run.started_at).total_seconds() > s.run_timeout_s + 15)
            if run is None or (run.status != "queued" and not stale):
                # idempotent: duplicate deliveries of a queued/finished/in-progress run do nothing
                log(logger, logging.WARNING, "run not runnable; skipping", status=getattr(run, "status", None))
                return None
            if stale:  # worker died mid-run (acks_late redelivery): take the run over, discarding the partial steps
                db.query(Step).filter(Step.run_id == run_id).delete()
                log(logger, logging.WARNING, "taking over stale run")
            run.status, run.started_at, run.trace_id = "running", utcnow(), current_trace_id()
            request, pv, created = dict(run.request_json), run.prompt_version, run.created_at
        t0 = time.time()
        try:
            store = get_store()
            if not store.ready:
                raise RuntimeError("corpus index is not built; run `python -m app.corpus.ingest`")
            ctx = RunContext(run_id, request, store, get_llm(), prompt_version=pv)
            result = run_graph(ctx)
            result["usage"] = _usage(ctx)
            result["run_id"] = run_id
            result["cache_hit"] = False
            err = None
        except Exception as e:  # unexpected failure: explicit failed state, never a confident answer
            log(logger, logging.ERROR, "run failed", err=type(e).__name__, detail=str(e)[:300])
            AGENT_TASKS.labels("pipeline", "error").inc()
            result, err = None, f"{type(e).__name__}: {str(e)[:300]}"
        finished = utcnow()
        total_ms = (finished - created).total_seconds() * 1000
        with session_scope() as db:
            run = db.get(Run, run_id)
            run.finished_at, run.total_latency_ms = finished, total_ms
            if result is None:
                run.status, run.error = "failed", err
            else:
                u = result["usage"]
                run.status, run.result_json, run.route = "completed", result, result["route"]
                run.degraded = result["completeness"] != "complete"
                run.prompt_tokens, run.completion_tokens, run.cost_usd = u["prompt_tokens"], u["completion_tokens"], u["cost_usd"]
                run.steps, run.tool_calls, run.retries = u["steps"], u["tool_calls"], u["retries"]
        RUN_EXEC.observe(time.time() - t0)
        RUN_DURATION.observe(total_ms / 1000)
        RUNS_TOTAL.labels("failed" if result is None else "completed", (result or {}).get("route") or "none", (result or {}).get("completeness") or "none").inc()
        if result and result["completeness"] == "complete" and persist_cache and s.run_cache_enabled:
            cache_set_json(cache_key(request, pv, result.get("corpus_version", "")), {"result": result, "source_run": run_id}, s.cache_ttl_s)
        log(logger, logging.INFO, "run finished", status="failed" if result is None else "completed", route=(result or {}).get("route"),
            total_ms=round(total_ms), steps=(result or {}).get("usage", {}).get("steps"))
        return result


def lookup_cached(request: dict, prompt_version: str) -> dict | None:
    cv = get_store().info.get("corpus_version", "")
    return cache_get_json(cache_key(request, prompt_version, cv))
