"""Measures the effect of the whole-run cache on latency and cost (D7) against a running stack.
   python scripts/measure_cache.py http://localhost:8000 20"""
import json
import statistics
import sys
import time
import uuid

import httpx

base, n = sys.argv[1], int(sys.argv[2])
c = httpx.Client(base_url=base, timeout=60)


def one(desc):
    t0 = time.time()
    r = c.post("/v1/export-checks", json={"product_description": desc}, headers={"X-Client-Id": "cache-bench"}).json()
    while c.get(r["status_url"]).json()["status"] not in ("completed", "failed"):
        time.sleep(0.25)
    d = c.get(r["result_url"]).json()
    return time.time() - t0, r["cache_hit"], d["usage"]["cost_usd"] if not r["cache_hit"] else 0.0


def p95(v):
    v = sorted(v)
    return v[min(len(v) - 1, int(0.95 * len(v)))]


cold = [one(f"Steel nails, 50 mm, for carpentry use, batch {uuid.uuid4().hex[:6]}") for _ in range(n)]
same = "Stainless steel vacuum flask with a moulded plastic outer body, cache probe " + uuid.uuid4().hex[:6]
warm = [one(same) for _ in range(n)]                       # 1 miss then n-1 hits
mixed = []
pool = [f"Steel nails, 50 mm, for carpentry use, pool {i}" for i in range(5)]
for i in range(n * 2):                                      # realistic repeat traffic over a small pool of descriptions
    mixed.append(one(pool[i % 5]))
out = {
    "uncached": {"n": n, "p50_s": round(statistics.median(x[0] for x in cold), 2), "p95_s": round(p95([x[0] for x in cold]), 2), "mean_cost_usd": round(statistics.mean(x[2] for x in cold), 6)},
    "repeat_same_request": {"n": n, "hits": sum(x[1] for x in warm), "p50_s": round(statistics.median(x[0] for x in warm), 3), "p95_s": round(p95([x[0] for x in warm]), 2), "mean_cost_usd": round(statistics.mean(x[2] for x in warm), 6)},
    "pool_of_5_repeating": {"n": len(mixed), "hit_ratio": round(sum(x[1] for x in mixed) / len(mixed), 3), "p50_s": round(statistics.median(x[0] for x in mixed), 3), "p95_s": round(p95([x[0] for x in mixed]), 2), "mean_cost_usd": round(statistics.mean(x[2] for x in mixed), 6)},
}
print(json.dumps(out, indent=2))
