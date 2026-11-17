#!/usr/bin/env bash
# Stepped-concurrency load test against the local stack (no Docker). Writes loadtest/results/w<W>_u<U>_*.csv
# plus cpu/queue snapshots. Usage: scripts/run_loadtest_matrix.sh "1 3" "2 4 8 12 16" 75
set -uo pipefail
cd "$(dirname "$0")/.."
WORKERS="${1:-1 3}"; USERS="${2:-2 4 8 12 16}"; DUR="${3:-75}"
mkdir -p loadtest/results
for W in $WORKERS; do
  scripts/run_local_stack.sh stop >/dev/null 2>&1; sleep 1
  LIVE_SCORE_SAMPLE_RATE=0 scripts/run_local_stack.sh start "$W" >/dev/null
  for U in $USERS; do
    TAG="w${W}_u${U}"
    ( for i in $(seq 1 $((DUR/5))); do
        sleep 5
        echo "t=$((i*5)) queue=$(redis-cli -p 6390 llen ecc) $(ps -eo pcpu,comm,args --sort=-pcpu | grep -E 'celery|uvicorn|locust' | grep -v grep | awk '{printf "%s:%s ", $2, $1}' | head -c 300)" >> "loadtest/results/${TAG}_snap.txt"
      done ) &
    SNAP=$!
    python3 -m locust -f loadtest/locustfile.py --headless -u "$U" -r "$U" -t "${DUR}s" --host http://localhost:8000 --csv "loadtest/results/${TAG}" --only-summary > "loadtest/results/${TAG}_out.txt" 2>&1
    wait $SNAP 2>/dev/null
    # drain: wait for queue to empty before next step
    for i in $(seq 1 60); do [ "$(redis-cli -p 6390 llen ecc)" = "0" ] && break; sleep 2; done; sleep 5
    echo "done $TAG: $(grep -E '^E2E' loadtest/results/${TAG}_out.txt)"
  done
done
scripts/run_local_stack.sh stop
