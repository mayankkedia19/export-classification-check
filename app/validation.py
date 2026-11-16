"""Ingestion boundary: schema/range/category/cross-field validation + guardrail screening + quarantine (F2, D1)."""
from __future__ import annotations

import logging
import uuid
from typing import Any

from pydantic import ValidationError

from app.db import Quarantine, session_scope
from app.guardrails import check_input_text
from app.logging_setup import log
from app.metrics import QUARANTINED
from app.schemas import ExportCheckRequest, FieldError

logger = logging.getLogger(__name__)


def validate_request(payload: Any) -> tuple[ExportCheckRequest | None, list[FieldError]]:
    if not isinstance(payload, dict):
        return None, [FieldError(field="$", code="type_error", message="body must be a JSON object")]
    try:
        req = ExportCheckRequest.model_validate(payload)
    except ValidationError as e:
        errs = []
        for err in e.errors():
            loc = ".".join(str(x) for x in err["loc"]) or "$"
            errs.append(FieldError(field=loc, code=err["type"], message=str(err["msg"]).removeprefix("Value error, ")))
        return None, errs
    errs = [
        FieldError(field=f, code=c, message=m)
        for f, c, m in check_input_text(
            {"product_description": req.product_description, "intended_use": req.intended_use,
             "materials": " ".join(req.materials)}
        )
    ]
    if errs:
        return None, errs
    return req, []


def quarantine(payload: Any, errors: list[FieldError], client_id: str, correlation_id: str) -> str:
    qid = uuid.uuid4().hex[:16]
    safe = payload if isinstance(payload, dict) else {"_raw_type": type(payload).__name__}
    with session_scope() as s:
        s.add(Quarantine(id=qid, correlation_id=correlation_id, client_id=client_id, payload_json=_clip(safe),
                         errors_json=[e.model_dump() for e in errors]))
    reason = errors[0].code.split(".")[0] if errors and errors[0].code.startswith("guardrail") else "validation"
    QUARANTINED.labels(reason=reason).inc()
    log(logger, logging.WARNING, "request quarantined", quarantine_id=qid, n_errors=len(errors),
        fields=[e.field for e in errors])
    return qid


def _clip(d: dict) -> dict:
    out = {}
    for k, v in list(d.items())[:30]:
        out[str(k)[:60]] = v if not isinstance(v, str) else v[:2000]
    return out
