# Evaluation (L2, L3, L5)

All numbers below were produced by `python -m eval.run` on this repository (results in `eval/results/*.json`) with the
**in-process deterministic stub model**, the **DEMO corpus** and the **hashing embedder**. They show that the harness,
the verification gate and the agent loop behave as designed. They are **not** estimates of accuracy on the official tariff
or with a real model: re-run with `LLM_PROVIDER=openai_compatible`, the official corpus and real embeddings before quoting any figure.

## The evaluation set (`eval/dataset.jsonl`, 32 scenarios)

Each scenario has the request, an optional injected fault, the expected outcome (route, heading, status or quarantine
error code) and `supports`: the corpus passages the answer must rest on. Categories: the section 4.1 case (E01),
article named in the tariff (E02-E11), capacity-dependent headings (E12-E14), ambiguous tie (E15), out of scope (E16-E18),
carve-out in the heading text (E19), residual-only / missing evidence (E20), a known vocabulary limitation (E21),
malformed requests (E22-E24), prompt injection (E25-E26), toxicity (E27), fabricated citations (E28, E31, E32),
provider failure (E29), malformed model output (E30).

## How each metric is computed (no LLM judge; `app/quality.py`)

| Metric | Computation |
|---|---|
| **Groundedness** | Split the rationale into sentences; a sentence is grounded if >= 60% of its content-word stems occur in the cited passages (text, clause, heading) or the request. Mean over runs with a proposal. Lexical proxy for a TruLens groundedness judge; it undercounts paraphrase and over-credits copied words |
| **Context relevance** | Mean cosine similarity between the request and every passage retrieved in the run (local embedder). With the hashing embedder this is lexical overlap, so absolute values are low; use it for comparisons, not as a quality bar |
| **Faithfulness** | Fraction of citations that resolve to a retrieved passage and quote it verbatim, set to 0 if the proposed heading code is not the code of a cited heading passage |
| **Hallucination rate** | Share of runs with any unresolved or misquoted citation, or a proposed code that is not a heading in the corpus, in the *returned* result. `pre_verification_hallucination_rate` is the same test on the first attempt, before the verifier acted |

Also reported: route accuracy, `unsafe_clear_rate` (cleared a case that should not be), escalation recall, gold-passage
retrieval and citation recall, first-attempt verification pass rate, steps / tool calls / LLM calls, cost.

## Result: full system (prompt v2, hybrid retrieval, verification on)


- `n`: **32**
- `passed`: **31**
- `accuracy`: **0.969**
- `route_accuracy`: **0.962**
- `unsafe_clear_rate`: **0.0**
- `escalation_recall`: **1.0**
- `quarantine_accuracy`: **1.0**
- `agent_runs_started_for_rejected_requests`: **0**
- `groundedness`: **0.7244**
- `context_relevance`: **0.1193**
- `faithfulness`: **1.0**
- `hallucination_rate`: **0.0**
- `pre_verification_hallucination_rate`: **0.115**
- `gold_passage_retrieval_recall`: **0.9886**
- `gold_passage_citation_recall`: **0.9205**
- `first_attempt_verification_pass_rate`: **0.885**
- `mean_attempts`: **1.1154**
- `mean_llm_calls`: **11.7692**
- `mean_tool_calls`: **8.6538**

Failed case: ['E21'] (E21: "double-wall vacuum insulated bottle" is not literally named by heading 9617, which says "vacuum flasks and other vacuum vessels"; the system falls back to the residual heading and escalates instead of answering. A real model or a synonym list would fix it. It is kept in the set so the number is honest.)

## L3: one deliberate change, all four metrics before and after

**Change: remove the verification gate** (`VERIFY_ENABLED=false`, ablation only), i.e. return whatever Agent 1 proposes. It was chosen because the retrieval-mode change I tried first (semantic vs hybrid) moved none of the four metrics on this set, so it would have said nothing.

| metric | before (no verification) | after (verification on) |
|---|---|---|
| groundedness | 0.6859 | 0.7244 |
| context_relevance | 0.1189 | 0.1193 |
| faithfulness | 0.9423 | 1.0 |
| hallucination_rate | 0.115 | 0.0 |
| accuracy | 0.875 | 0.969 |
| mean LLM calls per run | 6.7692 | 11.7692 |
| cost per 100 runs (USD, configured prices) | 0.6263 | 0.9257 |

The gate removes every fabricated citation from returned results (hallucination 0.115 -> 0.0, faithfulness 0.942 -> 1.0) at about +48% cost (more model turns). Only 3 of 32 scenarios inject a fabricated citation, so the absolute differences are small and the sample is tiny; the direction and mechanism are what the test shows.

## L2: prompt variants on the same set

v1 = retrieval-first, follow-up search only after the verifier asks; v2 = GRI-ordered, derives the follow-up search from what it read. **The stub honours the behaviour flags declared in each prompt's `meta.json` (it cannot read prose), so this comparison demonstrates the harness and the mechanism; the winner on a real model must be re-measured.**

| metric | v1 | v2 |
|---|---|---|
| accuracy | 0.969 | 0.969 |
| unsafe_clear_rate | 0.0 | 0.0 |
| groundedness | 0.7244 | 0.7244 |
| faithfulness | 1.0 | 1.0 |
| hallucination_rate | 0.0 | 0.0 |
| first_attempt_verification_pass_rate | 0.769 | 0.885 |
| mean_attempts | 1.2308 | 1.1154 |
| mean_llm_calls | 12.2692 | 11.7692 |
| mean_tool_calls | 8.9231 | 8.6538 |
| cost_usd_per_100_runs | 0.98 | 0.9257 |

**v2 performed better on first-attempt verification pass rate (0.769 -> 0.885), LLM calls per run (12.2692 -> 11.7692) and cost per 100 runs (0.98 -> 0.9257, 5.5% lower); accuracy, unsafe-clear rate and hallucination rate were identical.** v1 reaches the same answers but needs a second attempt on cases whose proposal lacks the exclusion note (the verifier catches it, e.g. the flask case).

## Single-pass baseline (assignment section 5)

Embed the request, take the top-scoring heading, answer, no tools and no verification (`--baseline`, 21 routed scenarios):
accuracy **0.381**, unsafe-clear rate **1.0** (it always answers). Caveat: with the lexical hashing embedder this baseline happens to find heading 9617 for the flask description because the words "vacuum flask" overlap; the failure described in section 5 is a property of dense embeddings and could not be reproduced here.

## L5: cost

At the configured example prices (0.0005/1k prompt tokens, 0.0015/1k completion tokens, env-configurable), cost per 100 runs is **USD 0.9257** on this set (a stub reports estimated token counts: characters / 4). **Largest cost driver: `research.decide`**, the research agent's decide turns.
Cost share by step: {"research.decide": 0.763, "verifier.decide": 0.221, "research.extract_entities": 0.016, "research.search_heading_names": 0.0}. Tool calls cost nothing; the verifier's turns are the second driver.
Every run records prompt tokens, completion tokens and cost per step (`GET /v1/runs/{id}/trace`, `usage.by_step`) at call time.

## Live scoring (L6)

The worker scores a sample (`LIVE_SCORE_SAMPLE_RATE`, default 0.25) of completed runs with the same metrics, stores them (`quality_scores`), exposes `GET /v1/quality` and the Grafana panel "Live-sampled quality". Tested in `tests/test_api.py::test_live_quality_scoring`.

## Reproduce

```
python -m app.corpus.ingest
python -m eval.run --prompt-version v2 --out eval/results/with_verification.json
VERIFY_ENABLED=false python -m eval.run --prompt-version v2 --out eval/results/without_verification.json     # PowerShell: $env:VERIFY_ENABLED="false"
python -m eval.run --compare v1 v2 --out eval/results/prompt_compare.json
python -m eval.run --baseline
```
