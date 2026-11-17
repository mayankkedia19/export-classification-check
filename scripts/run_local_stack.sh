#!/usr/bin/env bash
# Runs the whole system WITHOUT Docker (Redis + stub LLM + API + N workers) for development and for measuring on a
# machine without a Docker daemon. Compose is the supported deployment; this mirrors its process topology.
#   scripts/run_local_stack.sh start [WORKERS]   |   scripts/run_local_stack.sh stop
set -euo pipefail
cd "$(dirname "$0")/.."
RUN=data/local; mkdir -p "$RUN"
export DATABASE_URL="${DATABASE_URL:-sqlite:///$PWD/data/local/app.db}" REDIS_URL=redis://localhost:6390/0 INDEX_DIR="${INDEX_DIR:-$PWD/data/index}" \
  LLM_PROVIDER=openai_compatible LLM_BASE_URL=http://localhost:8001/v1 CELERY_EAGER=false RATE_LIMIT_PER_MIN=100000 \
  LIVE_SCORE_SAMPLE_RATE="${LIVE_SCORE_SAMPLE_RATE:-0.25}" APP_ENV=dev PYTHONPATH="$PWD"
case "${1:-start}" in
 start)
  N="${2:-1}"
  redis-server --port 6390 --save "" --appendonly no --daemonize yes --pidfile "$PWD/$RUN/redis.pid" --logfile "$PWD/$RUN/redis.log" >/dev/null
  python3 -m app.corpus.ingest >/dev/null
  python3 -c "from app.db import init_db; init_db()"
  nohup uvicorn app.stubllm.service:app --port 8001 --log-level warning >"$RUN/stub.log" 2>&1 & echo $! >"$RUN/stub.pid"
  nohup uvicorn app.main:app --port 8000 --log-level warning --workers 1 >"$RUN/api.log" 2>&1 & echo $! >"$RUN/api.pid"
  for i in $(seq 1 "$N"); do
    WORKER_METRICS_PORT=$((9100+i)) nohup celery -A app.worker.celery_app worker -P threads -c "${WORKER_CONCURRENCY:-4}" -Q ecc -n "w$i@%h" --loglevel=WARNING >"$RUN/worker$i.log" 2>&1 & echo $! >"$RUN/worker$i.pid"
  done
  for i in $(seq 1 40); do curl -sf localhost:8000/ready >/dev/null && break; sleep 0.5; done
  echo "up: api :8000, stub :8001, redis :6390, workers=$N (threads x ${WORKER_CONCURRENCY:-4})";;
 stop)
  for f in "$RUN"/*.pid; do [ -f "$f" ] && kill "$(cat "$f")" 2>/dev/null || true; done
  pkill -f "celery -A app.worker" 2>/dev/null || true
  rm -f "$RUN"/*.pid; echo stopped;;
esac
