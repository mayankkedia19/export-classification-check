"""Agent 1: Classification Research Agent. Extracts entities, then loops: the model chooses which tool to call next
(retrieval is a tool, not a fixed stage) until it can emit a structured, cited proposal."""
from __future__ import annotations

import logging

from pydantic import ValidationError

from app.agents.context import RunContext, StepLimitExceeded
from app.agents.tools import ToolBox
from app.corpus import heuristics as H
from app.llm.base import LLMFormatError
from app.schemas import Proposal

logger = logging.getLogger(__name__)
STRATEGIES = ["article_first", "semantic_expand", "lexical_broad"]


def extract_entities(ctx: RunContext) -> dict:
    req = ctx.request_fields()
    try:
        e = ctx.llm_json("research", "extract", "extract_entities", {"task": "extract entities"})
    except LLMFormatError:  # one bounded repair attempt for malformed model output
        e = ctx.llm_json("research", "extract", "extract_entities", {"task": "extract entities", "repair": "previous output was not valid JSON"})
    mats = [m for m in e.get("materials", []) if isinstance(m, str)]
    # declared materials from the request are merged in deterministically
    for m in H.detect_materials(" ".join(ctx.request.get("materials") or []), None):
        if m not in mats:
            mats.append(m)
    ent = {
        "article": str(e.get("article") or "")[:120],
        "materials": mats,
        "function": e.get("function") or req.get("intended_use"),
        "attributes": {"capacity_litres": ctx.request.get("capacity_litres")},
        "missing": e.get("missing") or [],
        "material_chapters": sorted({H.MATERIAL_CHAPTER[m] for m in mats if m in H.MATERIAL_CHAPTER}),
    }
    ctx.entities = ent
    return ent


def _expand(ctx: RunContext, raw: dict) -> dict:
    """Turn the model's proposal into the Proposal contract. Citations are expanded from RETRIEVED passages only;
    an id that was never retrieved is kept as unresolved so verification can catch it."""

    def cite(c):
        pid = str(c.get("passage_id", ""))
        p = ctx.retrieved.get(pid)
        base = {"passage_id": pid, "quote": str(c.get("quote", ""))[:400]}
        if p:
            return {**base, "source_id": p["source_id"], "doc_type": p["doc_type"], "clause": p.get("clause"),
                    "heading_code": p.get("heading_code"), "page": p.get("page"), "version": p.get("version"), "official": bool(p.get("official"))}
        return {**base, "source_id": "UNRESOLVED", "doc_type": "unknown", "official": False}

    d = {k: raw.get(k) for k in ("heading_code", "heading_title", "gri_applied", "rationale", "status", "export_policy", "missing_information", "candidates")
         if raw.get(k) is not None}
    d["citations"] = [cite(c) for c in raw.get("citations") or [] if isinstance(c, dict)]
    if raw.get("export_policy_citation"):
        d["export_policy_citation"] = cite(raw["export_policy_citation"])
    d.setdefault("status", "proposed")
    if d.get("gri_applied") is not None:
        d["gri_applied"] = str(d["gri_applied"])
    try:
        return Proposal.model_validate(d).model_dump()
    except ValidationError as e:
        return Proposal(status="incomplete", rationale=f"model output failed schema validation: {len(e.errors())} errors").model_dump()


def run_research(ctx: RunContext, strategy: str, attempt: int, feedback: list[str]) -> dict:
    tb = ToolBox(ctx, "research")
    if attempt == 1 or not ctx.entities:
        extract_entities(ctx)
    history: list[dict] = []
    repairs = 0
    for turn in range(ctx.s.max_agent_steps):
        ctx.bump_loop("research")
        state = {"strategy": strategy, "attempt": attempt, "feedback": feedback, "entities": ctx.entities, "history": history,
                 "tools": tb.specs(), "steps_left": ctx.s.max_agent_steps - turn, "tool_calls_left": ctx.s.max_tool_calls - ctx.tool_calls}
        try:
            act = ctx.llm_json("research", "research", "decide", state)
        except LLMFormatError:
            repairs += 1
            if repairs > 1:
                return Proposal(status="incomplete", rationale="model repeatedly returned malformed output").model_dump()
            continue
        kind = act.get("action")
        if kind == "tool":
            res = tb.call(str(act.get("tool")), act.get("args") if isinstance(act.get("args"), dict) else {})
            history.append({"tool": act.get("tool"), "args": act.get("args") or {}, "passages": res.get("passages", []),
                            "error": res.get("error"), "fields": res.get("fields")})
        elif kind == "final" and isinstance(act.get("proposal"), dict):
            return _expand(ctx, act["proposal"])
        elif kind == "out_of_scope":
            return _expand(ctx, {"status": "out_of_scope", "rationale": str(act.get("reason", ""))[:400]})
        else:
            repairs += 1
            if repairs > 1:
                return Proposal(status="incomplete", rationale="model returned an unknown action").model_dump()
    raise StepLimitExceeded("research: loop step limit reached without a proposal")
