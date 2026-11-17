"""Generates monitoring/grafana/dashboards/ecc.json (kept as a script so panel queries stay reviewable)."""
import json

panels = []
_id = [0]


def panel(title, exprs, x, y, w=8, h=7, unit="short", kind="timeseries"):
    _id[0] += 1
    panels.append({
        "id": _id[0], "title": title, "type": kind, "datasource": {"type": "prometheus", "uid": "prom"},
        "gridPos": {"x": x, "y": y, "w": w, "h": h}, "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "targets": [{"expr": e, "legendFormat": l, "refId": chr(65 + i)} for i, (e, l) in enumerate(exprs)],
    })


q = lambda p, m, r="2m": f"histogram_quantile({p}, sum(rate({m}_bucket[{r}])) by (le))"
# row 1: end to end SLO
panel("End-to-end run latency p50 / p95 (submission -> result); SLO p95 <= 20 s", [(q(0.5, "ecc_run_duration_seconds"), "p50"), (q(0.95, "ecc_run_duration_seconds"), "p95"), ("20", "SLO")], 0, 0, 12, unit="s")
panel("Throughput (finished runs / s)", [("sum(rate(ecc_runs_total[1m]))", "runs/s"), ("sum(rate(ecc_runs_total{status=\"failed\"}[1m]))", "failed/s")], 12, 0, 6, unit="ops")
panel("Availability (targets up)", [("avg(up{job=~\"api|worker\"})", "up ratio")], 18, 0, 6, unit="percentunit")
# row 2: http
panel("API latency p50 / p95", [(q(0.5, "ecc_http_request_duration_seconds"), "p50"), (q(0.95, "ecc_http_request_duration_seconds"), "p95")], 0, 7, unit="s")
panel("API error rate (5xx / all)", [("sum(rate(ecc_http_requests_total{status=~\"5..\"}[1m])) / sum(rate(ecc_http_requests_total[1m]))", "5xx ratio")], 8, 7, unit="percentunit")
panel("Quarantined / rate-limited / cache hits", [("sum(rate(ecc_quarantined_total[5m]))", "quarantined/s"), ("rate(ecc_rate_limited_total[5m])", "429/s"), ("sum(rate(ecc_cache_events_total{kind=\"run\",result=\"hit\"}[5m]))", "run-cache hits/s")], 16, 7, unit="ops")
# row 3: agents (A6)
panel("Task success rate per agent", [("sum by (agent)(rate(ecc_agent_tasks_total{outcome=\"success\"}[5m])) / sum by (agent)(rate(ecc_agent_tasks_total[5m]))", "{{agent}}")], 0, 14, unit="percentunit")
panel("Steps per run (mean) per agent", [("sum by (agent)(rate(ecc_agent_steps_per_run_sum[5m])) / sum by (agent)(rate(ecc_agent_steps_per_run_count[5m]))", "{{agent}}")], 8, 14)
panel("Tool calls per run (mean) per agent", [("sum by (agent)(rate(ecc_agent_tool_calls_per_run_sum[5m])) / sum by (agent)(rate(ecc_agent_tool_calls_per_run_count[5m]))", "{{agent}}")], 16, 14)
panel("Latency per step, p95 (LLM decide / tools)", [("histogram_quantile(0.95, sum by (le, agent, kind)(rate(ecc_step_duration_seconds_bucket[2m])))", "{{agent}} {{kind}}")], 0, 21, unit="s")
panel("LLM retry rate and breaker state (0 closed, 1 half-open, 2 open)", [("rate(ecc_llm_retries_total[2m])", "retries/s {{instance}}"), ("max(ecc_llm_breaker_state)", "breaker")], 8, 21)
panel("Verification outcomes / degraded runs", [("sum by (outcome)(rate(ecc_verification_total[5m]))", "verify {{outcome}}"), ("sum by (reason)(rate(ecc_degraded_runs_total[5m]))", "degraded {{reason}}")], 16, 21, unit="ops")
# row 4: cost (L5) and quality (L6)
panel("LLM cost per step (USD/s)", [("sum by (step)(rate(ecc_llm_cost_usd_total[5m]))", "{{step}}")], 0, 28, unit="currencyUSD")
panel("Tokens/s by kind", [("sum by (kind)(rate(ecc_llm_tokens_total[5m]))", "{{kind}}")], 8, 28)
panel("Live-sampled quality (rolling mean of latest scored runs)", [("ecc_quality_score", "{{metric}}")], 16, 28, unit="percentunit")
# row 5: containers (D5)
panel("CPU per container (cores)", [("sum by (name)(rate(container_cpu_usage_seconds_total{name=~\".+\"}[1m]))", "{{name}}")], 0, 35)
panel("Memory per container", [("sum by (name)(container_memory_working_set_bytes{name=~\".+\"})", "{{name}}")], 8, 35, unit="bytes")
panel("Container uptime", [("time() - container_start_time_seconds{name=~\".+\"}", "{{name}}")], 16, 35, unit="s")
# row 6: corpus freshness (D2)
panel("Corpus: chunks / documents / official ratio", [("ecc_corpus_chunks", "chunks"), ("ecc_corpus_documents", "documents"), ("ecc_corpus_official_chunk_ratio", "official ratio")], 0, 42)
panel("Corpus age (s since index build)", [("time() - ecc_corpus_built_timestamp_seconds", "age")], 8, 42, unit="s")
panel("Ingestion throughput (chunks embedded / s)", [("ecc_ingest_chunks_per_second", "chunks/s")], 16, 42)

dash = {"title": "Export Classification Check", "uid": "ecc", "schemaVersion": 39, "version": 1, "refresh": "5s",
        "time": {"from": "now-30m", "to": "now"}, "panels": panels, "tags": ["ecc"]}
json.dump(dash, open("monitoring/grafana/dashboards/ecc.json", "w"), indent=1)
print(len(panels), "panels")
