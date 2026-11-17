# Service level objective (declared before any load measurement)

**Declared:** 2026-10-13, before the first Locust run. Nothing in this file was changed after measuring; measured
results are reported separately in `docs/LOADTEST.md`.

## The objective

| Item | Target |
|---|---|
| Latency | **p95 <= 20 s**, measured from submission (`POST /v1/export-checks`) to the moment the final result is retrievable (`status = completed`), not the submission call alone |
| Load | 1 completed run per second sustained (60 runs/min) |
| Provider | stub LLM service, 600 ms +/- 200 ms per call (a typical small hosted model) |
| Availability of successful runs | >= 99% of submitted runs end `completed` (an escalation is a successful run; `failed` is not) |

## Why 20 s (against assignment section 1)

A classification check sits in the broker's pre-filing workflow: a wrong or slow answer there is what holds a
consignment at customs, but the analyst is waiting at the screen, so the check must finish inside an interactive
attention span. A run is about 11-12 model calls (extract, up to nine research turns, up to four verifier turns; see
the evaluation). At 0.6 s per call that is about 7 s of model time, leaving headroom for queueing, retrieval and
one verification retry (a retry adds about 5 s). 20 s tolerates one retry plus queueing and stays inside "tens of
seconds" quoted in assignment A4. 1 run/s is an order of magnitude above a single analyst team's rate (tens of
checks per hour), so meeting it with margin means the service is not the bottleneck for a realistic user base.

## Method

- Locust, closed loop: each virtual user submits, polls until `completed`, records the whole interval as one
  `e2e_run` sample, thinks, repeats. Product descriptions carry a unique suffix so the run cache cannot hide agent cost.
- Concurrency is stepped up to find the level at which p95 crosses the target and which component is saturated.
- Scaling test: the same step with 1 and 3 worker replicas.
