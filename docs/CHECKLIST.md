# Requirement checklist (assignment section 4, 6, 7, 8, 9)

Status key: **T** implemented and exercised by an automated test or a measured run; **R** implemented and run manually/measured;
**N** implemented but **not run** in the build environment; **P** partial (see note).

## Functional (F)
| # | Requirement | Status | Where / evidence |
|---|---|---|---|
| F1 | REST interface + published contract | T | `POST /v1/export-checks`, `GET /v1/contract`, docs/API.md; `test_end_to_end_async_flow` |
| F2 | Schema/range/categorical/cross-field validation, quarantine | T | `schemas.py`, `validation.py`; `test_rejects_with_field_errors`, `test_malformed_request_quarantined_and_no_run_started` |
| F3 | Identify article/materials, extract entities | P | `extract_entities` (result `entities`); tested with the deterministic stub's extractor; a real model's extraction quality is unmeasured |
| F4 | Candidate headings incl. headings naming the article, with notes | T | `search_heading_names`, `list_applicable_notes`; `test_vacuum_flask_case` |
| F5 | Proposal grounded in rule, heading, notes, with citations | T | `test_vacuum_flask_case` (citations resolve to retrieved text) |
| F6 | Route to one of three outcomes with basis | T | `agents/routing.py`; tests for clear / clarify / specialist |
| F7 | Escalate on tie after GRI 3, or outside the chapters | T | `test_tie_escalates`, `test_out_of_scope_escalates`, `test_glass_inner_carved_out_is_out_of_scope` |
| F8 | Health, metrics, structured logs | T / N | `/health`, `/ready`, `/metrics` tested; JSON logs with correlation id seen in worker/api logs; scraping by Prometheus not run (no Docker) |

## Agent system (A)
| # | Requirement | Status | Notes |
|---|---|---|---|
| A1 | LangGraph; retrieval is a tool; >= 3 tools; bounded loop (steps, tool calls, wall clock) | T | 8 tools; `test_tool_budget_bounds_the_run`, `test_wall_clock_bound`, `test_tool_timeout_becomes_observation_not_crash` |
| A2 | >= 2 agents, hand-off, topology and mid-run failure documented | T | docs/ARCHITECTURE.md; `test_provider_down_degrades...`, `test_verifier_failure_never_clears` |
| A3 | Verify every answer; citations resolve; retry with another strategy or escalate | T | `test_fabricated_citation_caught_then_recovered`, `test_unverifiable_answer_is_withheld` |
| A4 | FastAPI, async with run id, polling, queue between submission and execution; justified | T / R | Celery+Redis stack run locally (queued -> running -> completed observed); justification in docs/ARCHITECTURE.md |
| A5 | Timeouts, bounded retry/backoff, circuit breaker, degradation path | T | `test_retry_then_breaker_opens`, `test_breaker_half_open_recovers`, demo item 8 via `/admin/chaos` |
| A6 | Per-agent success rate, steps/tool calls, latency, retry rate, uptime on a dashboard | P / N | Metrics implemented and exposed; Grafana dashboard JSON generated and syntax-valid but **not rendered** (no Docker); container uptime comes from cAdvisor |
| A7 | `--scale worker=3` works; measure throughput and p95 | R | Measured with 1 vs 3 worker processes (not containers): docs/LOADTEST.md. `docker compose up --scale worker=3` itself not run |
| A8 | Simple web client | R | `app/web/index.html`, served at `/` (smoke-tested headless, see docs/LOADTEST.md note) |

## LLMOps (L)
| # | Requirement | Status | Notes |
|---|---|---|---|
| L1 | LangChain + FAISS, chunking/embedding documented, re-embedding without full rebuild | T | `LegalRetriever`, docs/CORPUS.md, `test_ingest_is_idempotent_and_incremental` |
| L2 | Versioned prompts, version on every response, two variants compared | T | `app/prompts/v1,v2`; docs/EVALUATION.md (stub-honouring-flags caveat) |
| L3 | >= 20 scenarios, batch CLI, groundedness/context relevance/faithfulness/hallucination, one change before/after | T | 32 scenarios; TruLens **not** used: equivalent local metrics, computation stated in docs/EVALUATION.md |
| L4 | Injection defence on every free-text field, output filtering, toxicity detection on input and output | T / P | `guardrails.py`; tests. Toxicity is a lexicon (misses subtle toxicity); injection is pattern-based (misses novel phrasing) |
| L5 | Tokens/cost per step and per run, cost per 100 runs, largest driver | T | docs/EVALUATION.md (stub token counts are estimates, prices are configured examples) |
| L6 | Feedback endpoint, aggregation, live scoring of a sample, quality over time | T / N | `test_feedback_and_summary`, `test_live_quality_scoring`; the Grafana "over time" chart not rendered |

## Platform (D)
| # | Requirement | Status | Notes |
|---|---|---|---|
| D1 | Data contract, enforced at the boundary, quarantine, malformed request starts no agent | T | `test_malformed_request_quarantined_and_no_run_started` |
| D2 | Idempotent corpus CLI; freshness and throughput as metrics | T | `/v1/corpus/status`, `ecc_corpus_*`, `ecc_ingest_chunks_per_second` |
| D3 | >= 4 services, one `docker compose up`, health checks, boundaries justified | N / P | 10 services declared with health checks and `depends_on` conditions; `docker compose config` passes; **never started** (no Docker daemon). Boundaries justified in docs/ARCHITECTURE.md |
| D4 | No credentials/endpoints/model ids in the repo; `.env.example` | T | `.env.example` placeholders; settings read the environment; `.gitignore` excludes `.env` |
| D5 | Structured logs, correlation id, OTel to Jaeger, Prometheus, Grafana, run reconstructible | P | Logs, correlation id (api -> task -> worker), trace endpoint and span creation tested/run; **OTLP export to Jaeger and Grafana panels not run** |
| D6 | SLO declared first; submission-to-result measurement; Locust with stub; RPS/p50/p95; failing concurrency and component | R | docs/SLO.md (declared before measuring), docs/LOADTEST.md |
| D7 | Caching justified; rate limiting per client; measured effect | T / R | `test_run_cache_hit_has_no_agent_cost`, `test_rate_limit_per_client`; measurement in docs/LOADTEST.md |
| D8 | Runbook for two failure modes; incident log with a genuine entry | T | docs/RUNBOOK.md, docs/INCIDENTS.md (4 real entries) |

## Demonstration items (section 9)
1 compose from a clean clone **N** | 2 routed result with citations **T** | 3 traced run **T** | 4 quarantine, no run **T** | 5 escalation instead of fabrication **T** |
6 self-verification catches an unsupported answer **T** | 7 injection blocked **T** | 8 breaker opens, degradation **T** | 9 Locust vs target **R** | 10 the section 4.1 case **T** (DEMO corpus)
