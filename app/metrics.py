"""Prometheus metrics. API and worker processes each expose their own subset (D5, A6, L5, D2)."""
from prometheus_client import Counter, Gauge, Histogram

_LAT = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60)
_RUN = (0.5, 1, 2, 3, 5, 8, 12, 16, 20, 30, 45, 60, 120)

# --- API ---
HTTP_REQUESTS = Counter("ecc_http_requests_total", "HTTP requests", ["route", "method", "status"])
HTTP_LATENCY = Histogram("ecc_http_request_duration_seconds", "HTTP latency", ["route", "method"], buckets=_LAT)
RUNS_SUBMITTED = Counter("ecc_runs_submitted_total", "Accepted runs", ["cache"])
QUARANTINED = Counter("ecc_quarantined_total", "Requests quarantined at the ingestion boundary", ["reason"])
RATE_LIMITED = Counter("ecc_rate_limited_total", "Requests rejected by the rate limiter")
CACHE_EVENTS = Counter("ecc_cache_events_total", "Cache hits/misses", ["kind", "result"])
FEEDBACK = Counter("ecc_feedback_total", "Feedback submissions", ["satisfactory"])

# --- corpus / ingestion (D2) ---
CORPUS_CHUNKS = Gauge("ecc_corpus_chunks", "Chunks in the index")
CORPUS_DOCS = Gauge("ecc_corpus_documents", "Source documents in the index")
CORPUS_BUILT_TS = Gauge("ecc_corpus_built_timestamp_seconds", "Unix time the index was last built")
CORPUS_OFFICIAL = Gauge("ecc_corpus_official_chunk_ratio", "Share of chunks from official sources")
INGEST_CPS = Gauge("ecc_ingest_chunks_per_second", "Ingestion throughput (chunks embedded per second)")

# --- worker / agents (A6, L5) ---
RUNS_TOTAL = Counter("ecc_runs_total", "Finished runs", ["status", "route", "completeness"])
RUN_DURATION = Histogram("ecc_run_duration_seconds", "Submission -> final result", buckets=_RUN)
RUN_EXEC = Histogram("ecc_run_execution_seconds", "Worker execution time only", buckets=_RUN)
AGENT_TASKS = Counter("ecc_agent_tasks_total", "Agent task outcomes", ["agent", "outcome"])
AGENT_STEPS = Histogram("ecc_agent_steps_per_run", "Loop steps per run", ["agent"], buckets=(1, 2, 3, 4, 5, 6, 8, 10, 15, 20))
AGENT_TOOLCALLS = Histogram("ecc_agent_tool_calls_per_run", "Tool calls per run", ["agent"], buckets=(0, 1, 2, 3, 4, 6, 8, 12, 16))
STEP_LATENCY = Histogram("ecc_step_duration_seconds", "Per-step latency", ["agent", "kind", "name"], buckets=_LAT)
TOOL_CALLS = Counter("ecc_tool_calls_total", "Tool calls", ["agent", "tool", "status"])
LLM_CALLS = Counter("ecc_llm_calls_total", "LLM calls", ["status"])
LLM_RETRIES = Counter("ecc_llm_retries_total", "LLM call retries")
BREAKER_STATE = Gauge("ecc_llm_breaker_state", "0=closed 1=half-open 2=open")
BREAKER_OPENED = Counter("ecc_llm_breaker_opened_total", "Times the breaker opened")
DEGRADED = Counter("ecc_degraded_runs_total", "Runs served via the degradation path", ["reason"])
VERIFY_OUTCOMES = Counter("ecc_verification_total", "Verification outcomes per attempt", ["outcome"])
GUARDRAIL_HITS = Counter("ecc_guardrail_hits_total", "Guardrail interventions", ["kind"])
TOKENS = Counter("ecc_llm_tokens_total", "Tokens", ["kind", "agent", "step"])
COST = Counter("ecc_llm_cost_usd_total", "LLM cost in USD at configured prices", ["agent", "step"])
QUALITY = Gauge("ecc_quality_score", "Live sampled quality (rolling mean of latest scored runs)", ["metric"])
