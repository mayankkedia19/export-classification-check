# REST API and data contract

Interactive docs: `http://localhost:8000/docs` (OpenAPI). Published request contract (JSON Schema):
`GET /v1/contract`. Contract version **1.0**. Every response carries `X-Correlation-ID` (send your own to join logs).

## `POST /v1/export-checks` -> `202`

Request (`extra` fields are rejected):

| field | type | rule |
|---|---|---|
| `product_description` | string, required | 10-1500 chars, readable text; screened for prompt injection and toxicity |
| `materials` | list[string] | max 8, each 2-40 chars (lower-cased) |
| `intended_use` | string | max 300; screened |
| `capacity_litres` | number | 0 to 1,000,000 |
| `weight_grams` | number | > 0 to 1,000,000 |
| `quantity` | integer | 1 to 1,000,000 (default 1) |
| `unit_value` / `currency` | number / enum INR USD EUR GBP AED SGD | **cross-field:** both or neither |
| `destination_country` | string | `^[A-Z]{2}$` (default `US`) |
| `export_intent` | enum commercial / sample / return | **cross-field:** `sample` requires quantity <= 100 |

Headers: `X-Client-Id` (rate-limit identity; falls back to the source IP; `X-API-Key` identity when keys are configured),
`X-API-Key` (required only if `API_KEYS` is set), `X-Correlation-ID` (optional).

Responses: `202 RunAccepted {run_id, status, status_url, result_url, trace_url, correlation_id, cache_hit}`;
`422 ErrorResponse` with `quarantine_id` and `field_errors[]` (the record is quarantined and **no agent run starts**);
`429` with `Retry-After`; `503 queue_unavailable`. A whole-run cache hit returns `202` with `status: completed`, `cache_hit: true`.

## Runs

| Endpoint | Purpose |
|---|---|
| `GET /v1/runs/{id}` | status: `queued` -> `running` -> `completed` or `failed`, timestamps, `total_latency_ms` (submission to final result), route |
| `GET /v1/runs/{id}/result` | `200` result when finished; `202` while queued/running; failed runs return `status: failed` with no classification |
| `GET /v1/runs/{id}/trace` | every step: agent, kind (llm/tool/handoff/verify/route/system), input, output, passage ids, latency, tokens, cost, prompt version; plus the verification outcome |
| `POST /v1/runs/{id}/feedback` | `{satisfactory: bool, reason: str<=500}` |

Result fields: `route`, `route_basis`, `escalation_reason`, `proposal {heading_code, heading_title, gri_applied, rationale, confidence, status, candidates[], citations[], export_policy_citation, missing_information}`,
`entities`, `verification {passed, checks[], issues[], attempts, history[]}`, `completeness` (`complete`/`degraded`/`incomplete`),
`corpus_mode`, `corpus_version`, `prompt_version`, `warnings[]`, `usage {tokens, cost_usd, llm_calls, tool_calls, steps, retries, by_step}`, `disclaimer`.
Each citation carries `passage_id, source_id, doc_type, clause, heading_code, page, version, official, quote`; the quote is verbatim from retrieved text.

## Operations

| Endpoint | Purpose |
|---|---|
| `GET /health`, `GET /ready` | liveness; readiness (database, Redis, corpus index) with `503` if not ready |
| `GET /metrics` | Prometheus (workers expose theirs on `:9101`) |
| `GET /v1/corpus/status` | mode (demo/official/mixed), chunks, documents, built_at, embedding, sources/versions, ingest throughput |
| `GET /v1/feedback/summary` | counts, satisfaction rate, by route, top reasons |
| `GET /v1/quality` | live-sampled quality scores over time |
| `GET /v1/stats` | run counts by status, quarantine count, mean cost per uncached run |
| `GET /v1/quarantine`, `/v1/quarantine/{id}` | rejected payloads with field errors (**admin key**) |
| `POST/GET /admin/chaos` | fault injection for the demo: `provider_down`, `slow`, `fabricate_citation`, `malformed_json`, `none` (**admin key**) |

Admin endpoints need `X-Admin-Key` equal to `ADMIN_API_KEY`; outside `APP_ENV=dev` they are disabled when the key is unset.

## Errors

`{"error": "...", "message": "...", "correlation_id": "...", "quarantine_id": "...", "field_errors": [{"field","code","message"}], "retry_after_s": 5}`
