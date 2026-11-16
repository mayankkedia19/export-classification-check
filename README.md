# Export Classification Check Service

A multi-agent service that takes a product description, validates it, proposes a Harmonized System (HS) tariff heading
(Customs Tariff Chapters 73 and 96) **with the General Rule applied and cited evidence**, states the export policy, and routes
the result to `clear-for-filing`, `seek-product-clarification` or `specialist-classification-review`. It never files a
declaration; a licensed customs broker decides. Built for ML System Design & AgentOps (UE24AM342AA5), Mini Project 07.

> **Corpus status: DEMO.** The official CBIC / DGFT documents could not be downloaded in the build environment, so the
> repository ships a clearly labelled demo corpus (approximate paraphrase, `official: false`). Every result and the UI
> say so. Load the official PDFs before relying on any output: [docs/CORPUS.md](docs/CORPUS.md).

## What you get

| | |
|---|---|
| API | FastAPI, async runs (`202` + run id, poll), published data contract, quarantine of rejected requests, trace, feedback, health/ready/metrics |
| Agents | LangGraph: Agent 1 (Research) and Agent 2 (Verification and Routing), 8 tools, explicit hand-off, bounded loops, retry with a different strategy, escalation |
| Retrieval | LangChain retriever over FAISS + BM25 (hybrid), legal-unit chunks with context, incremental re-embedding, official-PDF/OCR ingestion |
| Reliability | timeouts, bounded retry with backoff, circuit breaker, degradation path, prompt-injection/toxicity/output guardrails, rate limit, caches |
| Ops | Postgres, Redis + Celery workers (`--scale worker=N`), Prometheus, Grafana (21 panels), Jaeger/OpenTelemetry, cAdvisor, Locust |
| Quality | 52 pytest tests, 32-scenario evaluation set with groundedness/context-relevance/faithfulness/hallucination metrics, prompt comparison |

## Quick start (Docker, from a clean clone) - PowerShell

```powershell
git clone <your repo url> ecc; cd ecc
copy .env.example .env                 # placeholders only; edit passwords if you like
docker compose up --build              # postgres, redis, indexer, stub-llm, api, worker, prometheus, grafana, jaeger, cadvisor
```

- App + UI: http://localhost:8000  - API docs: http://localhost:8000/docs
- Grafana: http://localhost:3000 (user/password from `.env`; dashboard "Export Classification Check")
- Jaeger: http://localhost:16686  - Prometheus: http://localhost:9090
- Scale agent workers: `docker compose up --build --scale worker=3`

The default model is the bundled **stub LLM service** (deterministic, free, 600 ms delay). To use a real provider set
`LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` in `.env` (any OpenAI-compatible endpoint; free tier or a local runtime) and set a
spending limit on that account first.

> Docker Compose was written and syntax-checked (`docker compose config`) but **could not be started in the build sandbox
> (no Docker daemon)**. Everything else was run: the same topology (Redis, stub service, API, Celery workers) was started
> without containers with `scripts/run_local_stack.sh` and used for the end-to-end checks and the load test.

## Quick start without Docker (single process, development)

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
$env:CELERY_EAGER = "true"; $env:LLM_PROVIDER = "stub_inprocess"; $env:EMBEDDING_BACKEND = "hashing"
python -m app.corpus.ingest                       # build the index (idempotent)
uvicorn app.main:app --port 8000                  # open http://localhost:8000
```
`CELERY_EAGER=true` executes the run inside the request, which is for development only; production topology uses the queue.
Linux/macOS: `scripts/run_local_stack.sh start 2` starts Redis, the stub service, the API and 2 workers (needs `redis-server`).

## Try it

```powershell
$H = "http://localhost:8000"
$r = Invoke-RestMethod -Method Post "$H/v1/export-checks" -ContentType "application/json" `
     -Body (@{product_description="Stainless steel vacuum flask with a moulded plastic outer body."; materials=@("stainless steel","plastic")} | ConvertTo-Json)
do { Start-Sleep -Milliseconds 700; $s = Invoke-RestMethod "$H$($r.status_url)" } while ($s.status -in "queued","running")
$d = Invoke-RestMethod "$H$($r.result_url)"; $d.route; $d.proposal.heading_code; $d.proposal.citations | Format-Table passage_id,clause,quote
```
curl: `curl -X POST localhost:8000/v1/export-checks -H "Content-Type: application/json" -d '{"product_description":"Steel nails, 50 mm, for carpentry use"}'`.
Full ten-item demonstration script: [docs/DEMO.md](docs/DEMO.md).

## The required case (assignment 4.1)

"Stainless steel vacuum flask with a moulded plastic outer body." The agent does not stop at the steel chapter. It
searches for a heading that *names* the article (finds 9617 "Vacuum flasks and other vacuum vessels ..."), reads the
section/chapter notes, derives a follow-up search from what it read (a Section XV note removes Chapter 96 articles from the
steel chapter), retrieves GRI 1 and the export policy, then the verifier checks every citation against retrieved text
and that no better-named heading exists. Result: `clear-for-filing`, heading 9617, GRI 1, citations to GRI 1 / heading / note /
policy. With prompt `v1` the verifier catches the missing note on the first attempt and the second attempt fixes it
(`pytest tests/test_core.py -k flask`).

## Commands

| Task | Command |
|---|---|
| Build / refresh the corpus index | `python -m app.corpus.ingest` (`--reembed`, `--reembed-source ID`, `--rebuild`) or `docker compose run --rm indexer` |
| Tests | `python -m pytest -q` |
| Evaluation | `python -m eval.run --compare v1 v2` ; `python -m eval.run --baseline` ; see [docs/EVALUATION.md](docs/EVALUATION.md) |
| Load test | `locust -f loadtest/locustfile.py --headless -u 12 -r 12 -t 90s --host http://localhost:8000` ; see [docs/LOADTEST.md](docs/LOADTEST.md) |
| Regenerate dashboard | `python scripts/make_dashboard.py` |

## Documentation

[Architecture and deployment boundaries](docs/ARCHITECTURE.md) | [API and data contract](docs/API.md) | [Corpus, chunking, embeddings](docs/CORPUS.md) |
[SLO](docs/SLO.md) | [Load test results](docs/LOADTEST.md) | [Evaluation](docs/EVALUATION.md) | [Runbook](docs/RUNBOOK.md) |
[Incident log](docs/INCIDENTS.md) | [Demo script](docs/DEMO.md) | [Requirement checklist](docs/CHECKLIST.md)

## Configuration

All environment-specific values come from environment variables (`.env.example` lists every one with a placeholder). No
credentials, endpoints or model identifiers are in the repository. Prompts are versioned artifacts in `app/prompts/<version>/`; the
prompt version is recorded with every step and response (`PROMPT_VERSION`).

## Layout

```
app/            main.py (API) worker.py (Celery) pipeline.py (run lifecycle) schemas.py validation.py guardrails.py
  agents/       graph.py research.py verifier.py verification.py routing.py fallback.py tools.py context.py
  corpus/       ingest.py pdfparse.py store.py embeddings.py heuristics.py
  llm/          providers.py resilience.py (timeout, backoff, breaker)    stubllm/ policy.py service.py
  prompts/      v1/ v2/            web/ index.html       cache.py ratelimit.py metrics.py tracing.py logging_setup.py quality.py db.py
corpus/         manifest.json  demo/passages.jsonl  official/ (put the CBIC/DGFT PDFs here)
eval/           dataset.jsonl run.py results/          loadtest/ locustfile.py        tests/
monitoring/     prometheus.yml grafana/                 docker-compose.yml Dockerfile  scripts/ docs/
```

## Known limitations (honest list)

- DEMO corpus and placeholder export-policy entries; real-document parsing is untested against the real layouts.
- The offline hashing embedder is lexical; dense embeddings (sentence-transformers) were not run here (model download blocked).
- Evaluation and load numbers use the deterministic stub, not a real model; they measure this system, not a provider.
- Heading matching is lexical and shared by both agents (a common-mode failure risk); a known miss is in the evaluation set (E21).
- Heading-level (4-digit) classification only; sub-heading (GRI 6) resolution and duty calculation are out of scope.
- Docker Compose was not started in the build environment (see above).
