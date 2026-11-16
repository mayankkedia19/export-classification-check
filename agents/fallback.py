"""Degradation path (A5): when the model provider is unavailable the service still runs deterministic retrieval, lists the
candidate headings with their evidence, and routes to a specialist. It never states a classification."""
from __future__ import annotations

from app.agents.context import RunContext
from app.agents.tools import ToolBox
from app.corpus import heuristics as H


def run_fallback(ctx: RunContext, reason: str) -> dict:
    tb = ToolBox(ctx, "research")
    saved = ctx.s.max_tool_calls
    res = tb.call("search_heading_names", {"text": ctx.request.get("product_description", "")})
    cands = [p for p in res.get("passages", []) if p.get("name_score", 0) >= 0.5][:4]
    cites = [{"passage_id": p["passage_id"], "quote": p["text"][:160], "source_id": ctx.retrieved[p["passage_id"]]["source_id"],
              "doc_type": "heading", "clause": p.get("clause"), "heading_code": p.get("heading_code"),
              "page": ctx.retrieved[p["passage_id"]].get("page"), "version": ctx.retrieved[p["passage_id"]].get("version"),
              "official": bool(p.get("official"))} for p in cands]
    mats = H.detect_materials(ctx.request.get("product_description", ""), ctx.request.get("materials"))
    ctx.entities = {"article": H.extract_article(ctx.request.get("product_description", ""), mats), "materials": mats, "function": None,
                    "attributes": {}, "missing": [], "material_chapters": sorted({H.MATERIAL_CHAPTER[m] for m in mats if m in H.MATERIAL_CHAPTER})}
    return {"status": "incomplete", "heading_code": None, "rationale": f"Automated analysis unavailable ({reason}). Candidate headings found by keyword match are listed without a determination.",
            "candidates": [{"heading_code": p["heading_code"], "heading_title": p["heading_title"], "name_score": p["name_score"]} for p in cands],
            "citations": cites, "confidence": 0.0}
