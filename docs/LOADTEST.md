# Load test results (D6, A7, D7)

Target (declared in `docs/SLO.md` before any measurement): **p95 <= 20 s end to end** (submission -> `completed`) at about
1 run/s sustained, stub LLM at 600 ms +/- 200 ms per call.

## Setup and honesty notes

- **Not run under Docker.** The build sandbox has no Docker daemon, so `docker compose up --scale worker=3` was not executed.
  The same process topology was run with `scripts/run_local_stack.sh`: Redis, the stub-LLM HTTP service, the API (1 uvicorn
  process), and **1 or 3 Celery worker processes** (each a thread pool of 4), SQLite instead of Postgres. Locust ran on the same
  2-vCPU / 8 GB machine, so absolute numbers include contention from the load generator.
- Locust closed loop (`loadtest/locustfile.py`): each user submits, polls `GET /v1/runs/{id}` every 0.5 s until `completed`,
  records the whole interval as one `E2E` sample, thinks 0.5-1.5 s. Unique description tail defeats the run cache. 75 s per step,
  all users spawned at once, so the first runs include a start-up transient; queue drained between steps. The 3-worker half had to
  be re-run after the sandbox session restarted mid-test; the table is from the complete re-run.
- One run is ~12 stub calls (~7 s). The stub timing is a model of a provider, not a measurement of one.
- Command: `scripts/run_loadtest_matrix.sh "1 3" "..." 75`. Raw output in `loadtest/results/`.

## Results

| workers (threads) | users | runs | failed | RPS (runs/s) | p50 s | p95 s | p95 <= 20 s |
|---|---|---|---|---|---|---|---|
| 1 (4) | 2 | 17 | 0 | 0.23 | 7.1 | 9.1 | met |
| 1 (4) | 4 | 35 | 0 | 0.47 | 7.1 | 8.6 | met |
| 1 (4) | 8 | 37 | 0 | 0.50 | 14.0 | 17.0 | met |
| 1 (4) | 12 | 37 | 0 | 0.50 | 21.0 | 24.0 | **not met** |
| 1 (4) | 16 | 37 | 0 | 0.50 | 29.0 | 34.0 | **not met** |
| 3 (12) | 8 | 69 | 0 | 0.93 | 7.1 | 9.0 | met |
| 3 (12) | 12 | 106 | 0 | 1.43 | 7.1 | 8.8 | met |
| 3 (12) | 16 | 113 | 0 | 1.53 | 9.1 | 12.0 | met |
| 3 (12) | 24 | 113 | 0 | 1.52 | 14.0 | 16.0 | met |
| 3 (12) | 32 | 107 | 0 | 1.44 | 19.0 | 23.0 | **not met** |

(Locust rounds percentiles to bucket boundaries, so high-end p50/p95 are approximate to about +/-0.5 s.)

**Was the target met?** At the target load of about 1 run/s: with 1 worker **no** (throughput saturates at 0.50 runs/s, so 1 run/s
cannot be sustained); with 3 workers **yes** (1.43 runs/s at 12 users with p95 8.8 s; the p95 target holds up to 24 users).
**Concurrency at which the target fails:** 12 users with 1 worker; 32 users with 3 workers.

## Bottleneck and the effect of scaling

- A run is almost entirely **waiting on model calls**, so the ceiling is `worker threads / run time`: 4 threads / ~7.3 s ~ 0.55
  runs/s (measured 0.50); 12 threads / ~7.5 s ~ 1.6 runs/s (measured 1.5). **The component responsible is agent-worker
  thread capacity.**
- Queue depth confirms it: with 1 worker the Redis `ecc` queue averaged 4 / 7.8 / 11.7 at 8 / 12 / 16 users (users minus 4 in
  flight); with 3 workers 2.9 / 11.8 / 18.9 at 16 / 24 / 32 users (users minus 12). p50 then rises by one run time per queued
  position.
- Not CPU: process CPU samples (`ps`, which averages over process lifetime, so a coarse indicator) showed each worker around
  5% and the API a few percent; the load generator was the largest consumer. Not the API (submission returned in milliseconds) and not Redis.
- **`--scale worker=3`:** peak throughput 0.50 -> 1.5 runs/s (3x, linear), and p95 at 12 users 24 s -> 8.8 s. Scaling helps because the
  bottleneck is worker concurrency, not a shared component. A real provider's rate limit would become the next ceiling, which
  is what the stub removes and what the per-client rate limit and circuit breaker protect against.
- Beyond 3 workers the next suspects are database write rate (about 24 `steps` rows per run) and the single API process;
  neither was tested to saturation.

## Caching (D7)

Measured with `scripts/measure_cache.py` against 3 workers, sequential requests (`loadtest/results/cache_effect.json`):

| scenario | requests | cache hit ratio | p50 s | p95 s | mean cost per run (USD, configured prices) |
|---|---|---|---|---|---|
| unique descriptions (cold) | 12 | 0 | 6.94 | 7.74 | 0.00848 |
| the same request repeated | 12 | 11/12 | 0.017 | 7.44* | 0.00087 |
| 5 descriptions repeating | 24 | 0.79 | 0.017 | 7.16* | 0.00176 |

\* With 12-24 samples the p95 is the single cache miss; hits answer in about 17 ms with no model call. **Effect:** a hit removes the
whole agent run (about 7 s and the full cost); cost per run falls in proportion to the hit ratio (79% hits -> about 79% lower cost).
Why whole-run caching: retrieval takes milliseconds next to model calls, so caching retrieval alone would not move p95 or cost.
Only complete (non-degraded) runs are cached, and the key includes the prompt version and corpus version, so a new tariff
edition or prompt change invalidates it. Query embeddings use an in-process LRU (`ecc_cache_events_total{kind="embedding"}`); its effect
was not separately measured (the hashing embedder takes microseconds).

## Rate limiting

Per-client fixed window (`RATE_LIMIT_PER_MIN`, default 30; the load test raises it so the limiter does not mask capacity).
`tests/test_api.py::test_rate_limit_per_client`: a looping client gets `429` with `Retry-After` after the limit while another
client is unaffected. At the default, one client is capped at about 30 x 0.0085 = USD 0.26 per minute at the configured prices.

## Web client smoke test

`app/web/index.html` was driven in headless Chromium (Playwright): the flask example gave `clear-for-filing`, heading 9617, the citations
table, verification checks and a 24-step trace; the malformed example showed the quarantine id; the injection example showed
`guardrail.prompt_injection`; no JavaScript errors.
