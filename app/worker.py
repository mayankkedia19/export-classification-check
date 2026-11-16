"""Celery worker: the queue/task runner between submission and execution (A4).
Run: celery -A app.worker.celery_app worker -P threads -c 4 -Q ecc"""
import logging
import random

from celery import Celery
from celery.signals import worker_ready

from app.config import get_settings
from app.logging_setup import setup_logging
from app.tracing import setup_tracing

s = get_settings()
setup_logging("ecc-worker", s.log_level)
setup_tracing("ecc-worker")
logger = logging.getLogger(__name__)

celery_app = Celery("ecc", broker=s.redis_url or "memory://", backend=None)
celery_app.conf.update(
    task_default_queue="ecc",
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_always_eager=s.celery_eager,
    task_eager_propagates=False,
    broker_connection_retry_on_startup=True,
    task_serializer="json",
    accept_content=["json"],
    result_backend=None,
    task_ignore_result=True,
)


@celery_app.task(name="ecc.run_export_check")
def run_export_check(run_id: str, correlation_id: str, carrier: dict | None = None):
    from app.pipeline import execute_run

    execute_run(run_id, correlation_id, carrier)
    if random.random() < get_settings().live_score_sample_rate:
        score_run.delay(run_id, correlation_id)


@celery_app.task(name="ecc.score_run")
def score_run(run_id: str, correlation_id: str = "-"):
    """Score a sample of live runs against the evaluation metrics (L6)."""
    from app.db import QualityScore, Run, Step, session_scope
    from app.corpus.store import get_store
    from app.metrics import QUALITY
    from app.quality import mean, score_result

    store = get_store()
    with session_scope() as db:
        run = db.get(Run, run_id)
        if not run or run.status != "completed" or not run.result_json:
            return
        ids = []
        for st in db.query(Step).filter(Step.run_id == run_id, Step.kind == "tool").all():
            ids += (st.output_json or {}).get("passages", [])
        retrieved = {i: store.by_id[i] for i in dict.fromkeys(ids) if i in store.by_id}
        sc = score_result(run.request_json, run.result_json, retrieved, store)
        db.add(QualityScore(run_id=run_id, groundedness=sc["groundedness"] if sc["groundedness"] is not None else 1.0,
                            context_relevance=sc["context_relevance"] or 0.0,
                            faithfulness=sc["faithfulness"] if sc["faithfulness"] is not None else 1.0, hallucinated=sc["hallucinated"]))
        db.flush()
        rows = db.query(QualityScore).order_by(QualityScore.id.desc()).limit(50).all()
        QUALITY.labels("groundedness").set(mean([r.groundedness for r in rows]) or 0)
        QUALITY.labels("context_relevance").set(mean([r.context_relevance for r in rows]) or 0)
        QUALITY.labels("faithfulness").set(mean([r.faithfulness for r in rows]) or 0)
        QUALITY.labels("hallucination_rate").set(sum(1 for r in rows if r.hallucinated) / len(rows))


@worker_ready.connect
def _on_ready(**_):
    from prometheus_client import start_http_server

    from app.db import init_db
    from app.corpus.store import get_store
    from app.metrics import CORPUS_CHUNKS

    try:
        init_db()
    except Exception as e:
        logger.warning("init_db at worker start failed: %s", type(e).__name__)
    st = get_store().status()
    CORPUS_CHUNKS.set(st["chunks"])
    try:
        start_http_server(get_settings().worker_metrics_port)
        logger.info("worker metrics on :%s", get_settings().worker_metrics_port)
    except OSError:
        logger.warning("worker metrics port busy")
