# Architecture

## Topology and deployment boundaries (D3)

```
                         +-------------------------------- docker compose ---------------------------------+
 browser / client        |                                                                                  |
 (web UI, curl) --HTTP-->|  api (FastAPI)            redis                    worker x N (Celery, threads)   |
                         |  - ingestion boundary     - Celery broker/queue    - Agent 1 Research             |
                         |  - validate+quarantine    - run/embedding cache    - Agent 2 Verification+Routing |
                         |  - rate limit             - rate-limit counters    - tools, LangGraph             |
                         |  - status/result/trace    - fault-injection flag   - corpus index (read only)     |
                         |        |  enqueue ---------------^   consume ------------^                        |
                         |        v                                                    |                    |
                         |  postgres  <------- runs, steps, quarantine, feedback, quality --------+         |
                         |                                                              LLM calls |         |
                         |  indexer (one shot) -> index volume       stub-llm (OpenAI-compatible) <+  or a   |
                         |                                           real provider by configuration         |
                         |  prometheus <- api:8000, worker:9101 (all replicas), cadvisor  -> grafana         |
                         |  jaeger <- OTLP traces from api and workers (trace context travels in the task)  |
                         +----------------------------------------------------------------------------------+
```

| Boundary | Why it is a boundary |
|---|---|
| **api / worker** | Different resource profiles (api: short IO-bound requests; worker: long multi-step runs holding model connections), independent scaling (`--scale worker=N`), failure isolation (a stuck run cannot block submission or status polling), independent release |
| **redis** | Decouples submission from execution (queue); also holds the caches, rate-limit counters and the fault flag shared by all replicas |
| **postgres** | Durable job state and the full run record; survives worker and api restarts |
| **indexer** | The corpus pipeline is a batch job with its own cadence (new tariff edition) and heavy dependencies (OCR, embedding model); it produces an immutable index volume the api and workers mount read-only |
| **stub-llm** | The provider is a network dependency. A separate service lets load tests replace the model with fixed answers after a realistic delay, and lets chaos tests take it down without touching our code |
| **monitoring** | Prometheus/Grafana/Jaeger/cAdvisor can be absent without affecting the service |

## Why asynchronous with polling (A4)

A run is about 12 sequential model calls, tens of seconds on a real provider. Holding an HTTP request open that long
breaks proxies and clients and ties a web worker to a run. `POST /v1/export-checks` therefore validates, persists a
`queued` run, enqueues it and returns `202` with a run id in milliseconds; the caller polls `GET /v1/runs/{id}`.
Polling was chosen over callbacks because a callback URL supplied by a caller is an SSRF vector and needs retry/delivery
infrastructure; polling needs neither and the web client can do it trivially. The queue is Celery on Redis, between
submission and execution.

## The two agents (A1, A2)

| | Agent 1: Classification Research | Agent 2: Verification and Routing |
|---|---|---|
| Responsibility | extract entities; find headings, notes, rules; propose with citations | distrust the proposal; check every citation; look for better-named or competing headings and missing notes; decide pass/fail and what to try next |
| Prompt | `prompts/<v>/research.md`, `extract.md` | `prompts/<v>/verify.md` |
| Tools | `search_corpus`, `search_heading_names`, `lookup_clause`, `list_applicable_notes`, `get_request_fields` | the same retrieval tools plus `verify_citations`, `check_competing_headings`, `check_note_coverage` |
| Loop bound | `MAX_AGENT_STEPS` turns | `MAX_AGENT_STEPS` turns |
| Sees | the request and its own observations | only the handed-off proposal and the evidence store, not Agent 1's reasoning |

Shared bounds per run: `MAX_TOOL_CALLS` (30), `RUN_TIMEOUT_S` (60 s wall clock), `MAX_RESEARCH_ATTEMPTS` (3), per-tool
timeout `TOOL_TIMEOUT_S` (5 s), per-model-call timeout `LLM_TIMEOUT_S` (20 s, never beyond the run deadline).

**Hand-off.** After Agent 1 returns, the graph writes an explicit `handoff` step (status, heading, citation ids,
attempt, strategy) and passes only the proposal object to Agent 2.

**Graph** (`app/agents/graph.py`, LangGraph):

```
START -> research -> handoff -> verify -> route -> END
           ^   |                  |
           |   +-- provider down -+--> degrade -> route
           +---- retry (new strategy) <-- verification failed, attempts and time left
           research --limit/timeout/guardrail--> route (escalate)
```

**Retrieval is a tool the agent chooses to call.** The research model decides each turn: which tool, with what
query. For the vacuum flask the first useful action is `search_heading_names` (does any heading name the article?),
then it reads the notes of the candidate's chapter, then derives a follow-up query from what it read ("does a note
remove Chapter 96 articles from the steel chapter?") that the request never mentioned, then retrieves GRI 1 and the
export policy. Strategy changes on retry (`article_first`, `semantic_expand`, `lexical_broad`).

**If an agent fails mid-run (A2).**
- Agent 1 cannot get the model (breaker open / retries exhausted): deterministic retrieval-only fallback lists
  candidate headings with evidence; route `specialist-classification-review`; no heading is stated; `completeness: degraded`.
- Agent 1 hits a step, tool, time or guardrail limit: escalate with `completeness: incomplete`.
- Agent 2 fails: the deterministic checks still gate the answer; the route can never be `clear-for-filing`.
- Worker dies: Celery redelivers (`acks_late`); a run left `running` past `RUN_TIMEOUT_S + 15 s` is taken over.
- Unexpected exception: run is `failed` with no result; the result endpoint returns no classification.

## Verification (A3): a model can downgrade a result but never upgrade one

`app/agents/verification.py` is plain code. The verifier agent chooses when to call it, and the graph re-runs it after the loop
as the authoritative gate: `passed = deterministic_checks AND agent_verdict != fail`. Checks: citations present,
each resolves to a passage retrieved in this run, quotes appear verbatim in the cited passage, the cited heading is the
proposed one, the heading's own terms name the article, GRI 1 is cited (and GRI 3 if applied), no heading anywhere in
the corpus names the article better or equally well, the heading does not carve the article out, and, when the article is
classified outside its material's chapter, a retrieved and cited note excludes it from that chapter. Export-policy
citation must resolve too. Confidence is computed from this evidence, never taken from the model.

Failed verification -> another attempt with a different strategy and the issues as feedback -> if attempts or time run
out, the proposal is **withheld** (`proposal: null`) and the run escalates with the failed checks as the reason.

## Routing (F6, F7)

| Outcome | When |
|---|---|
| `clear-for-filing` | verified, one heading best supported, confidence >= 0.75, second-agent verification ran |
| `seek-product-clarification` | verified, but the answer depends on information the request lacks (e.g. capacity decides between two headings) or confidence is below 0.75 |
| `specialist-classification-review` | two headings equally supported after GRI 3 (tie, not broken automatically), outside Chapters 73/96, only a residual heading matched, verification failed, degraded or incomplete run |

## Data flow and records (D5)

Every step (LLM call, tool call, hand-off, verification, routing) is a `steps` row with agent, input, output (passage ids),
latency, tokens, cost and prompt version. `GET /v1/runs/{id}/trace` reconstructs the run; OpenTelemetry spans carry the
same structure and the correlation id; trace context is injected into the Celery task so api and worker spans join.

## Design decisions and what was rejected

| Decision | Rejected alternative and why |
|---|---|
| LangGraph for the loop | CrewAI: less control over bounded loops and explicit conditional retry edges |
| Own thin tool layer + JSON actions over an OpenAI-compatible API | Provider-specific function-calling: ties us to one vendor; spec asks the provider be swappable by configuration |
| Deterministic verification code the agent invokes as tools | An LLM-only verifier: a judge that can be talked into approving; here it can only downgrade |
| FAISS flat index + BM25 hybrid (reciprocal-rank fusion) | Chroma: extra service/state for ~60-3000 chunks; flat inner-product search is exact and incremental add/remove by id is supported |
| One chunk per legal unit (heading, note, rule) with context header | Fixed-size windows: split a note from the heading it governs and lose the chapter/section/clause that make it citable |
| Polling | Callbacks (SSRF, delivery guarantees) |
| Celery threads pool | Prefork: prometheus metrics per process would need multiprocess mode; the work is IO-bound on model calls |
| Whole-run cache + query-embedding cache | Retrieval cache: retrieval is cheap next to model calls, so the whole-run cache is where the cost is |
| Fixed-window per-client rate limit | Token bucket: more precise, but the purpose is cost control, not shaping |
| Lexical heading-name matching for the "does a heading name the article" test | Dense retrieval only: spec section 5 explains why it misses; lexical match on heading text is the direct implementation of GRI 1 "terms of the headings" |
