"""FastAPI service: documented REST interface, ingestion boundary, async run submission (A4, F1, F2, F8)."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import func

from app.cache import backend, cache_get_json, cache_set_json
from app.config import get_settings
from app.corpus.store import get_store
from app.db import Feedback, QualityScore, Quarantine, Run, Step, init_db, session_scope, utcnow
from app.logging_setup import correlation_id_var, log, run_id_var, setup_logging
from app.metrics import (CACHE_EVENTS, CORPUS_BUILT_TS, CORPUS_CHUNKS, CORPUS_DOCS, CORPUS_OFFICIAL, FEEDBACK, HTTP_LATENCY,
                         HTTP_REQUESTS, INGEST_CPS, RATE_LIMITED, RUNS_SUBMITTED)
from app.pipeline import lookup_cached
from app.prompts import available_versions
from app.ratelimit import check_rate_limit
from app.schemas import (CONTRACT_VERSION, ErrorResponse, ExportCheckRequest, FeedbackIn, RunAccepted, RunStatus, RunTrace, TraceStep)
from app.tracing import current_trace_id, setup_tracing
from app.validation import quarantine, validate_request

s = get_settings()
setup_logging(s.service_name, s.log_level)
setup_tracing(s.service_name)
logger = logging.getLogger(__name__)
WEB_DIR = os.path.join(os.path.dirname(__file__), "web")


def refresh_corpus_gauges() -> None:
    st = get_store().status()
    CORPUS_CHUNKS.set(st["chunks"])
    CORPUS_DOCS.set(st["documents"])
    CORPUS_OFFICIAL.set(st["official_chunk_ratio"])
    info = get_store().info
    if info.get("built_ts"):
        CORPUS_BUILT_TS.set(info["built_ts"])
    if info.get("chunks_per_second"):
        INGEST_CPS.set(info["chunks_per_second"])


@asynccontextmanager
async def lifespan(app: FastAPI):
    for i in range(10):
        try:
            init_db()
            break
        except Exception as e:  # database may still be starting
            log(logger, logging.WARNING, "database not ready", attempt=i, err=type(e).__name__)
            time.sleep(1.5)
    refresh_corpus_gauges()
    yield


app = FastAPI(title="Export Classification Check Service", version="1.0.0", lifespan=lifespan,
              description="Proposes an HS classification (Chapters 73 and 96) with cited evidence and a routing outcome. "
                          "Never files a customs declaration; a licensed customs broker decides.")
try:
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app, excluded_urls="metrics,health")
except Exception:  # pragma: no cover
    pass


@app.middleware("http")
async def observe(request: Request, call_next):
    cid = request.headers.get("X-Correlation-ID") or uuid.uuid4().hex[:16]
    correlation_id_var.set(cid)
    request.state.correlation_id = cid
    t0 = time.time()
    try:
        resp = await call_next(request)
    except Exception:
        log(logger, logging.ERROR, "unhandled error", path=request.url.path)
        resp = JSONResponse(ErrorResponse(error="internal_error", message="unexpected error", correlation_id=cid).model_dump(), status_code=500)
    route = request.scope.get("route")
    path = route.path if route else "unmatched"
    HTTP_REQUESTS.labels(path, request.method, str(resp.status_code)).inc()
    HTTP_LATENCY.labels(path, request.method).observe(time.time() - t0)
    resp.headers["X-Correlation-ID"] = cid
    return resp


# ---------- auth / identity ----------
def public_auth(x_api_key: str | None = Header(default=None)):
    keys = [k for k in get_settings().api_keys.split(",") if k.strip()]
    if keys and x_api_key not in keys:
        raise HTTPException(401, detail="missing or invalid X-API-Key")


def admin_auth(x_admin_key: str | None = Header(default=None)):
    st = get_settings()
    if st.admin_api_key:
        if x_admin_key != st.admin_api_key:
            raise HTTPException(403, detail="admin key required")
    elif st.app_env != "dev":
        raise HTTPException(403, detail="admin endpoints are disabled: ADMIN_API_KEY is not set")


def client_id(request: Request, x_client_id: str | None, x_api_key: str | None) -> str:
    if x_api_key:
        return "key:" + hashlib.sha256(x_api_key.encode()).hexdigest()[:12]
    return (x_client_id or (request.client.host if request.client else "anon"))[:80]


def err(status: int, error: str, message: str, request: Request, **kw) -> JSONResponse:
    return JSONResponse(ErrorResponse(error=error, message=message, correlation_id=request.state.correlation_id, **kw).model_dump(),
                        status_code=status, headers={"Retry-After": str(kw["retry_after_s"])} if kw.get("retry_after_s") else None)


# ---------- submission ----------
@app.post("/v1/export-checks", status_code=202, response_model=RunAccepted, tags=["runs"],
          responses={422: {"model": ErrorResponse}, 429: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
          summary="Submit a product description (data contract v" + CONTRACT_VERSION + ")")
async def submit(request: Request, x_client_id: str | None = Header(default=None), x_api_key: str | None = Header(default=None),
                 _=Depends(public_auth)):
    cid = request.state.correlation_id
    client = client_id(request, x_client_id, x_api_key)
    ok, limit, retry = check_rate_limit(client)
    if not ok:
        RATE_LIMITED.inc()
        return err(429, "rate_limited", f"limit of {limit} requests/min exceeded", request, retry_after_s=retry)
    try:
        payload = await request.json()
    except Exception:
        qid = quarantine({"_raw": "unparseable body"}, [__import__("app.schemas", fromlist=["FieldError"]).FieldError(field="$", code="json_invalid", message="body is not valid JSON")], client, cid)
        return err(422, "validation_failed", "body is not valid JSON", request, quarantine_id=qid)
    req, errs = validate_request(payload)
    if errs:
        qid = quarantine(payload, errs, client, cid)
        return err(422, "validation_failed", "request rejected at the ingestion boundary and quarantined; no agent run was started",
                   request, quarantine_id=qid, field_errors=[e.model_dump() for e in errs])
    st = get_settings()
    request_dict = req.model_dump(mode="json")
    run_id = uuid.uuid4().hex[:20]
    cached = lookup_cached(request_dict, st.prompt_version) if st.run_cache_enabled else None
    CACHE_EVENTS.labels("run", "hit" if cached else "miss").inc()
    base = dict(id=run_id, client_id=client, request_json=request_dict, prompt_version=st.prompt_version, correlation_id=cid, trace_id=current_trace_id())
    if cached:
        res = {**cached["result"], "run_id": run_id, "cache_hit": True}
        with session_scope() as db:
            db.add(Run(**base, status="completed", result_json=res, route=res["route"], cache_hit=True, started_at=utcnow(), finished_at=utcnow(), total_latency_ms=0.0))
            db.add(Step(run_id=run_id, seq=1, agent="api", kind="system", name="run_cache_hit", output_json={"source_run": cached.get("source_run")}, prompt_version=st.prompt_version))
        RUNS_SUBMITTED.labels("hit").inc()
        return _accepted(run_id, "completed", cid, True)
    with session_scope() as db:
        db.add(Run(**base, status="queued"))
    from opentelemetry.propagate import inject

    carrier: dict = {}
    inject(carrier)
    try:
        from app.worker import run_export_check

        run_export_check.apply_async(args=[run_id, cid, carrier], queue="ecc", retry=False)
    except Exception as e:
        with session_scope() as db:
            r = db.get(Run, run_id)
            r.status, r.error, r.finished_at = "failed", f"queue_unavailable: {type(e).__name__}", utcnow()
        log(logger, logging.ERROR, "enqueue failed", err=type(e).__name__)
        return err(503, "queue_unavailable", "could not enqueue the run; please retry", request, retry_after_s=5)
    RUNS_SUBMITTED.labels("miss").inc()
    with session_scope() as db:
        r = db.get(Run, run_id)
        status = r.status
    return _accepted(run_id, status, cid, False)


def _accepted(run_id, status, cid, cache_hit):
    return RunAccepted(run_id=run_id, status=status, correlation_id=cid, status_url=f"/v1/runs/{run_id}", result_url=f"/v1/runs/{run_id}/result",
                       trace_url=f"/v1/runs/{run_id}/trace", cache_hit=cache_hit)


def _get_run(run_id: str) -> Run:
    with session_scope() as db:
        r = db.get(Run, run_id)
        if not r:
            raise HTTPException(404, "run not found")
        return r


@app.get("/v1/runs/{run_id}", response_model=RunStatus, tags=["runs"])
def run_status(run_id: str):
    r = _get_run(run_id)
    return RunStatus(run_id=r.id, status=r.status, created_at=r.created_at, started_at=r.started_at, finished_at=r.finished_at, route=r.route,
                     error=r.error, prompt_version=r.prompt_version, correlation_id=r.correlation_id, total_latency_ms=r.total_latency_ms)


@app.get("/v1/runs/{run_id}/result", tags=["runs"], summary="200 when finished, 202 while queued/running")
def run_result(run_id: str, response: Response):
    r = _get_run(run_id)
    if r.status in ("queued", "running"):
        response.status_code = 202
        return {"run_id": r.id, "status": r.status}
    if r.status == "failed":
        return {"run_id": r.id, "status": "failed", "route": None, "error": r.error,
                "message": "The run did not complete. No classification is returned."}
    return r.result_json


@app.get("/v1/runs/{run_id}/trace", response_model=RunTrace, tags=["runs"],
         summary="Reconstruct the whole run: every tool call, query, passage, prompt version, latency, cost and the verification outcome")
def run_trace(run_id: str):
    with session_scope() as db:
        r = db.get(Run, run_id)
        if not r:
            raise HTTPException(404, "run not found")
        steps = db.query(Step).filter(Step.run_id == run_id).order_by(Step.seq).all()
        return RunTrace(run_id=r.id, correlation_id=r.correlation_id, trace_id=r.trace_id, prompt_version=r.prompt_version, status=r.status,
                        verification=(r.result_json or {}).get("verification"),
                        steps=[TraceStep(seq=x.seq, agent=x.agent, kind=x.kind, name=x.name, input=x.input_json, output=x.output_json,
                                         latency_ms=x.latency_ms, prompt_tokens=x.prompt_tokens, completion_tokens=x.completion_tokens,
                                         cost_usd=x.cost_usd, prompt_version=x.prompt_version, status=x.status, error=x.error) for x in steps])


@app.post("/v1/runs/{run_id}/feedback", status_code=201, tags=["feedback"])
def feedback(run_id: str, body: FeedbackIn):
    _get_run(run_id)
    with session_scope() as db:
        db.add(Feedback(run_id=run_id, satisfactory=body.satisfactory, reason=body.reason))
    FEEDBACK.labels(str(body.satisfactory).lower()).inc()
    return {"run_id": run_id, "recorded": True}


@app.get("/v1/feedback/summary", tags=["feedback"])
def feedback_summary():
    with session_scope() as db:
        total = db.query(func.count(Feedback.id)).scalar() or 0
        good = db.query(func.count(Feedback.id)).filter(Feedback.satisfactory.is_(True)).scalar() or 0
        by_route = db.query(Run.route, Feedback.satisfactory, func.count(Feedback.id)).join(Run, Run.id == Feedback.run_id).group_by(Run.route, Feedback.satisfactory).all()
        reasons = db.query(Feedback.reason, func.count(Feedback.id)).filter(Feedback.satisfactory.is_(False), Feedback.reason != "").group_by(Feedback.reason).order_by(func.count(Feedback.id).desc()).limit(10).all()
    return {"total": total, "satisfactory": good, "unsatisfactory": total - good, "satisfaction_rate": round(good / total, 3) if total else None,
            "by_route": [{"route": a, "satisfactory": b, "count": c} for a, b, c in by_route], "top_unsatisfactory_reasons": [{"reason": a, "count": b} for a, b in reasons]}


@app.get("/v1/quality", tags=["feedback"], summary="Live-sampled quality scores over time (L6)")
def quality(limit: int = 100):
    with session_scope() as db:
        rows = db.query(QualityScore).order_by(QualityScore.id.desc()).limit(min(limit, 500)).all()
    pts = [{"run_id": r.run_id, "at": r.created_at.isoformat(), "groundedness": r.groundedness, "context_relevance": r.context_relevance,
            "faithfulness": r.faithfulness, "hallucinated": r.hallucinated} for r in reversed(rows)]
    n = len(pts)
    return {"n": n, "points": pts, "means": {k: round(sum(p[k] for p in pts) / n, 3) for k in ("groundedness", "context_relevance", "faithfulness")} if n else {},
            "hallucination_rate": round(sum(p["hallucinated"] for p in pts) / n, 3) if n else None}


@app.get("/v1/quarantine", tags=["quarantine"], dependencies=[Depends(admin_auth)])
def quarantine_list(limit: int = 50):
    with session_scope() as db:
        rows = db.query(Quarantine).order_by(Quarantine.created_at.desc()).limit(min(limit, 200)).all()
        return [{"id": q.id, "created_at": q.created_at.isoformat(), "correlation_id": q.correlation_id, "client_id": q.client_id,
                 "errors": q.errors_json, "payload": q.payload_json} for q in rows]


@app.get("/v1/quarantine/{qid}", tags=["quarantine"], dependencies=[Depends(admin_auth)])
def quarantine_get(qid: str):
    with session_scope() as db:
        q = db.get(Quarantine, qid)
        if not q:
            raise HTTPException(404, "not found")
        return {"id": q.id, "created_at": q.created_at.isoformat(), "errors": q.errors_json, "payload": q.payload_json}


@app.get("/v1/stats", tags=["ops"], summary="Aggregates behind the dashboard: run counts, runs created for quarantined requests (always 0)")
def stats():
    with session_scope() as db:
        runs = db.query(func.count(Run.id)).scalar() or 0
        by = dict(db.query(Run.status, func.count(Run.id)).group_by(Run.status).all())
        q = db.query(func.count(Quarantine.id)).scalar() or 0
        cost = db.query(func.avg(Run.cost_usd)).filter(Run.cache_hit.is_(False), Run.status == "completed").scalar()
    return {"runs": runs, "by_status": by, "quarantined": q, "avg_cost_usd_per_uncached_run": round(cost or 0, 6)}


@app.get("/v1/corpus/status", tags=["corpus"])
def corpus_status():
    st = get_store(reload=True).status()
    refresh_corpus_gauges()
    return st


@app.get("/v1/contract", tags=["contract"], summary="Published request data contract (JSON Schema)")
def contract():
    return {"version": CONTRACT_VERSION, "request_schema": ExportCheckRequest.model_json_schema(), "prompt_versions": available_versions()}


# ---------- admin / chaos (demo of A5) ----------
@app.post("/admin/chaos", tags=["admin"], dependencies=[Depends(admin_auth)])
def chaos_set(body: dict):
    mode = body.get("mode")
    if mode not in ("provider_down", "slow", "fabricate_citation", "malformed_json", "none"):
        raise HTTPException(422, "mode must be one of provider_down|slow|fabricate_citation|malformed_json|none")
    if mode == "none":
        backend().delete("chaos:llm")
        return {"chaos": None}
    cfg = {"mode": mode, "count": int(body.get("count", 1000000 if mode in ("provider_down", "slow") else 1))}
    cache_set_json("chaos:llm", cfg, int(body.get("ttl_s", 600)))
    return {"chaos": cfg}


@app.get("/admin/chaos", tags=["admin"], dependencies=[Depends(admin_auth)])
def chaos_get():
    return {"chaos": cache_get_json("chaos:llm")}


# ---------- health / metrics ----------
@app.get("/health", tags=["ops"])
def health():
    return {"status": "ok"}


@app.get("/ready", tags=["ops"])
def ready(response: Response):
    checks = {}
    try:
        with session_scope() as db:
            db.execute(__import__("sqlalchemy").text("select 1"))
        checks["database"] = "ok"
    except Exception as e:
        checks["database"] = f"error:{type(e).__name__}"
    try:
        backend().ping()
        checks["redis"] = "ok" if get_settings().redis_url else "in-memory"
    except Exception as e:
        checks["redis"] = f"error:{type(e).__name__}"
    checks["corpus_index"] = "ok" if get_store().ready else "missing: run python -m app.corpus.ingest"
    good = all(v in ("ok", "in-memory") for v in checks.values())
    if not good:
        response.status_code = 503
    return {"status": "ready" if good else "not_ready", "checks": checks}


@app.get("/metrics", include_in_schema=False)
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(os.path.join(WEB_DIR, "index.html"))
