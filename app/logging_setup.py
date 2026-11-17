import contextvars
import json
import logging
import re
import sys
import time

correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="-")
run_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("run_id", default="-")

_SECRET = re.compile(r"(?i)(api[_-]?key|authorization|password|secret|token)(\"?\s*[:=]\s*\"?)([^\s\",}]+)")


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        msg = _SECRET.sub(r"\1\2***", record.getMessage())
        d = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "msg": msg,
            "correlation_id": correlation_id_var.get(),
            "run_id": run_id_var.get(),
        }
        extra = getattr(record, "extra_fields", None)
        if extra:
            d.update({k: v for k, v in extra.items() if k not in d})
        if record.exc_info:
            d["exc"] = self.formatException(record.exc_info)
        return json.dumps(d, default=str)


def setup_logging(service: str, level: str = "INFO") -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonFormatter(service))
    root.addHandler(h)
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "urllib3", "faiss"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def log(logger: logging.Logger, level: int, msg: str, **fields) -> None:
    """Structured log with extra fields. Never pass request bodies or secrets here."""
    logger.log(level, msg, extra={"extra_fields": fields})
