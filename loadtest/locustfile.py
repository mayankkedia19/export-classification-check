"""Locust load test (D6). Measures a run from submission to final result, not the submission call alone.

  locust -f loadtest/locustfile.py --headless -u 10 -r 2 -t 120s --host http://localhost:8000 --csv loadtest/results/u10
The LLM behind the service is the stub (fixed responses after a realistic delay), so this measures our system and not
a provider's rate limits. SLO_P95_S is checked at the end and the process exits non-zero when the target is missed."""
import os
import random
import time
import uuid

from locust import HttpUser, between, events, task

SLO_P95_S = float(os.getenv("SLO_P95_S", "20"))
POLL_S = float(os.getenv("POLL_INTERVAL_S", "0.5"))
RUN_TIMEOUT_S = float(os.getenv("RUN_WAIT_TIMEOUT_S", "120"))
THINK_MIN, THINK_MAX = float(os.getenv("THINK_MIN_S", "0.5")), float(os.getenv("THINK_MAX_S", "1.5"))

PRODUCTS = [
    "Stainless steel vacuum flask with a moulded plastic outer body",
    "Steel nails, 50 mm, for carpentry use",
    "Stainless steel hex bolts and nuts",
    "Steel ballpoint pen with pocket clip",
    "Refillable cigarette lighter with a metal body",
    "Plastic comb for hair",
    "Stainless steel kitchen colander",
    "Steel chain for lifting, welded links",
    "Combined ballpoint pen and cigarette lighter, steel body",
    "Cotton t-shirt, short sleeves",
]

_e2e = []


class Analyst(HttpUser):
    wait_time = between(THINK_MIN, THINK_MAX)

    @task
    def check_product(self):
        # unique tail (after a comma) defeats the whole-run cache so every request costs a full agent run
        body = {"product_description": f"{random.choice(PRODUCTS)}, lot {uuid.uuid4().hex[:8]}"}
        t0 = time.time()
        with self.client.post("/v1/export-checks", json=body, headers={"X-Client-Id": f"u{id(self)}"}, name="POST /v1/export-checks (submit only)", catch_response=True) as r:
            if r.status_code != 202:
                r.failure(f"submit {r.status_code}")
                return
            j = r.json()
        status = "queued"
        while time.time() - t0 < RUN_TIMEOUT_S:
            s = self.client.get(j["status_url"], name="GET /v1/runs/{id}")
            status = s.json().get("status") if s.status_code == 200 else "error"
            if status in ("completed", "failed"):
                break
            time.sleep(POLL_S)
        total_ms = (time.time() - t0) * 1000
        ok = status == "completed"
        if ok:
            _e2e.append(total_ms / 1000)
        events.request.fire(request_type="E2E", name="run submission->result", response_time=total_ms, response_length=0,
                            exception=None if ok else Exception(f"run ended as {status}"), context={})


@events.quitting.add_listener
def _check_slo(environment, **_):
    if not _e2e:
        return
    v = sorted(_e2e)
    p = lambda q: v[min(len(v) - 1, int(q * len(v)))]
    ok = p(0.95) <= SLO_P95_S
    print(f"\nE2E runs={len(v)} p50={p(0.5):.2f}s p95={p(0.95):.2f}s max={v[-1]:.2f}s target p95<={SLO_P95_S}s -> {'MET' if ok else 'NOT MET'}")
    if not ok:
        environment.process_exit_code = 3
