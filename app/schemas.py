"""Published data contracts (D1). Request contract version: 1.0."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CONTRACT_VERSION = "1.0"


class Route(str, Enum):
    CLEAR = "clear-for-filing"
    CLARIFY = "seek-product-clarification"
    SPECIALIST = "specialist-classification-review"


class ExportIntent(str, Enum):
    COMMERCIAL = "commercial"
    SAMPLE = "sample"
    RETURN = "return"


class ExportCheckRequest(BaseModel):
    """Inbound product description. Unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    product_description: str = Field(min_length=10, max_length=1500, description="Free-text product description")
    materials: list[str] = Field(default_factory=list, max_length=8, description="Declared materials, optional")
    intended_use: str | None = Field(default=None, max_length=300)
    capacity_litres: float | None = Field(default=None, ge=0, le=1_000_000)
    weight_grams: float | None = Field(default=None, gt=0, le=1_000_000)
    quantity: int = Field(default=1, ge=1, le=1_000_000)
    unit_value: float | None = Field(default=None, gt=0, le=1e9)
    currency: Literal["INR", "USD", "EUR", "GBP", "AED", "SGD"] | None = None
    destination_country: str = Field(default="US", pattern=r"^[A-Z]{2}$")
    export_intent: ExportIntent = ExportIntent.COMMERCIAL

    @field_validator("materials")
    @classmethod
    def _materials(cls, v: list[str]) -> list[str]:
        out = []
        for m in v:
            m = m.strip()
            if not (2 <= len(m) <= 40):
                raise ValueError("each material must be 2-40 characters")
            out.append(m.lower())
        return out

    @field_validator("product_description")
    @classmethod
    def _desc_has_letters(cls, v: str) -> str:
        if sum(c.isalpha() for c in v) < 8:
            raise ValueError("description must contain readable text")
        return v

    @model_validator(mode="after")
    def _cross_field(self):
        if self.unit_value is not None and self.currency is None:
            raise ValueError("currency is required when unit_value is given")
        if self.currency is not None and self.unit_value is None:
            raise ValueError("unit_value is required when currency is given")
        if self.export_intent == ExportIntent.SAMPLE and self.quantity > 100:
            raise ValueError("export_intent 'sample' is limited to quantity <= 100")
        return self


class FieldError(BaseModel):
    field: str
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: str
    message: str
    correlation_id: str | None = None
    quarantine_id: str | None = None
    field_errors: list[FieldError] = []
    retry_after_s: int | None = None


class RunAccepted(BaseModel):
    run_id: str
    status: str
    correlation_id: str
    status_url: str
    result_url: str
    trace_url: str
    cache_hit: bool = False


class Citation(BaseModel):
    passage_id: str
    source_id: str
    doc_type: str
    clause: str | None = None
    heading_code: str | None = None
    page: int | None = None
    version: str | None = None
    official: bool = False
    quote: str


class Candidate(BaseModel):
    heading_code: str
    heading_title: str
    name_score: float
    note: str | None = None


class Proposal(BaseModel):
    heading_code: str | None = None
    heading_title: str | None = None
    gri_applied: str | None = None
    rationale: str = ""
    confidence: float = 0.0
    status: Literal["proposed", "tie", "out_of_scope", "insufficient_information", "incomplete"] = "proposed"
    candidates: list[Candidate] = []
    citations: list[Citation] = []
    export_policy: str | None = None
    export_policy_citation: Citation | None = None
    missing_information: list[str] = []


class CheckResult(BaseModel):
    name: str
    passed: bool
    detail: str = ""


class Verification(BaseModel):
    passed: bool
    checks: list[CheckResult] = []
    issues: list[str] = []
    attempts: int = 0
    history: list[dict[str, Any]] = []


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    llm_calls: int = 0
    tool_calls: int = 0
    steps: int = 0
    retries: int = 0
    by_step: dict[str, dict[str, float]] = {}


class RunResult(BaseModel):
    run_id: str
    status: str
    completeness: Literal["complete", "degraded", "incomplete"] = "complete"
    route: Route | None = None
    route_basis: str = ""
    escalation_reason: str | None = None
    proposal: Proposal | None = None
    entities: dict[str, Any] = {}
    verification: Verification | None = None
    corpus_mode: str = "demo"
    corpus_version: str = ""
    prompt_version: str = ""
    warnings: list[str] = []
    disclaimer: str = (
        "Proposed classification only. No customs declaration has been or will be filed by this service. "
        "A licensed customs broker makes the determination."
    )
    cache_hit: bool = False
    usage: Usage = Usage()


class RunStatus(BaseModel):
    run_id: str
    status: str
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    route: str | None = None
    error: str | None = None
    prompt_version: str | None = None
    correlation_id: str | None = None
    total_latency_ms: float | None = None


class TraceStep(BaseModel):
    seq: int
    agent: str
    kind: str
    name: str
    input: Any = None
    output: Any = None
    latency_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    prompt_version: str | None = None
    status: str = "ok"
    error: str | None = None


class RunTrace(BaseModel):
    run_id: str
    correlation_id: str | None
    trace_id: str | None
    prompt_version: str | None
    status: str
    steps: list[TraceStep]
    verification: dict | None = None


class FeedbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    satisfactory: bool
    reason: str = Field(default="", max_length=500)
