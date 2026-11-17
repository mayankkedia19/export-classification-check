# Runbook: the two most likely failure modes

## 1. Model provider unavailable or slow (circuit breaker opens, runs degrade)

**Symptom.** Results arrive with `completeness: "degraded"`, route `specialist-classification-review`, a warning
"Model provider was unavailable", and no proposed heading. Users see many escalations and no clear-for-filing results.
Grafana: breaker state panel at 2, degraded-runs rate above 0, LLM retry rate high, end-to-end latency rising (retries
back off before the breaker opens).

**Confirm.**
1. `curl -s localhost:9101/metrics | grep -E "ecc_llm_breaker_state|ecc_llm_calls_total|ecc_degraded_runs_total"` on a worker
   (`docker compose exec worker ...`): `ecc_llm_breaker_state 2` = open; `ecc_llm_calls_total{status="error"}` climbing.
2. Worker logs (JSON): `docker compose logs worker | grep "circuit breaker OPEN"`.
3. Probe the provider directly from a worker container: `python -c "import httpx,os;print(httpx.get(os.environ['LLM_BASE_URL'].rsplit('/v1',1)[0]+'/health',timeout=3).status_code)"`
   (for a hosted provider use its own status page and check the key/quota).
4. Rule out fault injection: `curl -H "X-Admin-Key: $ADMIN_API_KEY" localhost:8000/admin/chaos` must return `{"chaos":null}`.

**Recover.**
1. Fix the cause (provider outage: wait or switch `LLM_BASE_URL`/`LLM_MODEL`/`LLM_API_KEY` in `.env`; quota/429: lower
   `RATE_LIMIT_PER_MIN` and the worker count; stray chaos flag: `POST /admin/chaos {"mode":"none"}`).
2. `docker compose up -d --force-recreate worker`. The breaker half-opens after `BREAKER_RESET_S` (15 s) and closes on
   the first successful call, so a restart is only needed to change configuration.
3. Re-submit the escalated requests (they are not cached while degraded). Degraded runs never produced a
   determination, so no wrong answer needs to be withdrawn.

## 2. Worker backlog (queue grows, p95 breaches the SLO)

**Symptom.** `status` stays `queued` for long periods; end-to-end p95 above 20 s while API latency is normal; Locust
or users report timeouts. Grafana: throughput flat at the worker ceiling, queue-bound latency.

**Confirm.**
1. Queue depth: `docker compose exec redis redis-cli llen ecc` (non-zero and growing = backlog).
2. Capacity: workers x `WORKER_CONCURRENCY` threads / mean run time is the ceiling in runs/s. Check
   `docker compose ps worker` replicas and the CPU panel (worker near 1 core = CPU-bound; low CPU with a deep queue =
   thread-bound waiting on the model).
3. Is a cost loop running? One client looping shows up as `ecc_rate_limited_total` rising or one `client_id` dominating
   `SELECT client_id,count(*) FROM runs WHERE created_at > now() - interval '10 min' GROUP BY 1 ORDER BY 2 DESC`.

**Recover.**
1. Scale out: `docker compose up -d --scale worker=3` (or more). Raise `WORKER_CONCURRENCY` when workers are idle-waiting.
2. If one client is flooding: lower `RATE_LIMIT_PER_MIN`, then `docker compose up -d api`.
3. If a worker dies mid-run the task is redelivered (`acks_late`). The task skips runs that are finished or
   in progress, and takes over a run left `running` for longer than `RUN_TIMEOUT_S + 15 s`, so a duplicate delivery
   cannot double-run a request and a crashed run is not lost.
4. After recovery confirm `ecc_run_duration_seconds` p95 is back under 20 s.

See `docs/INCIDENTS.md` for the log and template.
