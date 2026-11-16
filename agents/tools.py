"""Callable tools (A1). Retrieval is a tool the agent chooses to call, not a fixed stage."""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout

from app.agents.context import RunContext, ToolBudgetExceeded
from app.agents import verification as V
from app.corpus import heuristics as H
from app.metrics import TOOL_CALLS

logger = logging.getLogger(__name__)
_pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="tool")

TOOL_SPECS = {
    "search_corpus": {"args": {"query": "str", "k": "int 1-8 (default 5)", "doc_type": "gri|heading|section_note|chapter_note|export_policy (optional)",
                               "chapter": "str (optional)", "mode": "hybrid|semantic|lexical (optional)"},
                      "desc": "Search the tariff corpus. Returns passages with ids, clause, heading code and metadata."},
    "search_heading_names": {"args": {"text": "str (defaults to the request description)"},
                             "desc": "Find headings whose own terms NAME the described article (GRI 1), with carve-outs and capacity conditions."},
    "lookup_clause": {"args": {"ref": "passage id, clause label (e.g. 'GRI 3(a)') or heading code"}, "desc": "Fetch one clause by identifier."},
    "list_applicable_notes": {"args": {"chapter": "str"}, "desc": "List section and chapter notes that apply to a chapter."},
    "get_request_fields": {"args": {"fields": "list[str] (optional)"}, "desc": "Read the request's own (sanitised) fields."},
}
RESEARCH_TOOLS = set(TOOL_SPECS)
VERIFIER_TOOLS = {"search_corpus", "search_heading_names", "lookup_clause", "list_applicable_notes", "get_request_fields",
                  "verify_citations", "check_competing_headings", "check_note_coverage"}
VERIFIER_SPECS = {
    "verify_citations": {"args": {}, "desc": "Resolve every citation to retrieved text and check quotes verbatim."},
    "check_competing_headings": {"args": {}, "desc": "Independently search all headings for one that names the article better than the proposal."},
    "check_note_coverage": {"args": {}, "desc": "Check that notes excluding the proposed or a competing chapter were retrieved and cited."},
}


def summarise(p: dict, ctx: RunContext, with_name: bool = True) -> dict:
    out = {"passage_id": p["passage_id"], "doc_type": p["doc_type"], "clause": p.get("clause"), "chapter": p.get("chapter"),
           "heading_code": p.get("heading_code"), "heading_title": p.get("heading_title"), "text": p["text"][:420],
           "applies_to_chapters": p.get("applies_to_chapters") or [], "excludes_chapters": p.get("excludes_chapters") or [],
           "official": p.get("official", False)}
    if "score" in p:
        out["score"] = p["score"]
    if with_name and p["doc_type"] == "heading":
        ds = H.desc_stems(ctx.request.get("product_description", ""))
        sc, ph = H.name_match(ds, p["heading_title"])
        out["name_score"] = sc
        out["matched_terms"] = ph
        out["residual"] = H.is_residual(p["heading_title"])
        out["carved_out_by"] = H.is_excluded_by_heading(ctx.request.get("product_description", ""), p["heading_title"])
        cap = H.capacity_condition(p["heading_title"])
        out["capacity_condition"] = list(cap) if cap else None
    return out


class ToolBox:
    def __init__(self, ctx: RunContext, agent: str):
        self.ctx, self.agent = ctx, agent
        self.allowed = RESEARCH_TOOLS if agent == "research" else VERIFIER_TOOLS
        self.proposal: dict | None = None  # set for the verifier

    def specs(self) -> dict:
        return {k: v for k, v in {**TOOL_SPECS, **VERIFIER_SPECS}.items() if k in self.allowed}

    def call(self, name: str, args: dict | None) -> dict:
        ctx, args = self.ctx, dict(args or {})
        if name not in self.allowed:
            TOOL_CALLS.labels(self.agent, name, "rejected").inc()
            ctx.record(self.agent, "tool", name, args, {"error": "tool not available to this agent"}, status="error", error="not_allowed")
            return {"error": f"tool {name!r} is not available to the {self.agent} agent"}
        if ctx.tool_calls >= ctx.s.max_tool_calls:
            raise ToolBudgetExceeded("tool-call budget exhausted")
        ctx.check_deadline()
        ctx.tool_calls += 1
        t0 = time.time()
        fut = _pool.submit(self._run, name, args)
        try:
            res = fut.result(timeout=min(ctx.s.tool_timeout_s, max(0.5, ctx.remaining())))
            status, err = "ok", None
        except FutTimeout:
            res, status, err = {"error": "tool timeout"}, "error", "timeout"
        except Exception as e:  # tool bugs/bad args become observations the agent can react to
            res, status, err = {"error": f"{type(e).__name__}: {e}"}, "error", type(e).__name__
        lat = (time.time() - t0) * 1000
        TOOL_CALLS.labels(self.agent, name, status).inc()
        out_rec = {"passages": [x["passage_id"] for x in res.get("passages", [])], **{k: v for k, v in res.items() if k != "passages"}}
        ctx.record(self.agent, "tool", name, args, out_rec, lat, status=status, error=err)
        return res

    # ---- implementations ----
    def _run(self, name: str, a: dict) -> dict:
        return getattr(self, "t_" + name)(**a)

    def _ret(self, passages: list[dict], **extra) -> dict:
        self.ctx.add_passages(passages)
        return {"passages": [summarise(p, self.ctx) for p in passages], **extra}

    def t_search_corpus(self, query: str, k: int = 5, doc_type=None, chapter=None, mode=None):
        k = max(1, min(int(k), 8))
        hits = self.ctx.store.search(str(query)[:400], k=k, mode=mode, doc_type=doc_type, chapter=chapter)
        return self._ret(hits, query=query)

    def t_search_heading_names(self, text=None):
        text = text or self.ctx.request.get("product_description", "")
        ds = H.desc_stems(text)
        rows = []
        for h in self.ctx.store.headings():
            sc, ph = H.name_match(ds, h["heading_title"])
            if sc >= 0.5:
                rows.append({**h, "score": sc})
        rows.sort(key=lambda r: (-r["score"], r["heading_code"]))
        return self._ret(rows[:6], query=text[:120])

    def t_lookup_clause(self, ref: str):
        p = self.ctx.store.get(str(ref))
        if not p:
            return {"passages": [], "error": f"no clause found for {ref!r}"}
        return self._ret([p])

    def t_list_applicable_notes(self, chapter: str):
        return self._ret(self.ctx.store.notes_for(str(chapter)), chapter=chapter)

    def t_get_request_fields(self, fields=None):
        f = self.ctx.request_fields()
        return {"fields": {k: v for k, v in f.items() if not fields or k in fields}}

    # ---- verifier tools (deterministic, evidence-producing) ----
    def t_verify_citations(self):
        return V.tool_result(V.check_citations(self.ctx, self.proposal or {}))

    def t_check_competing_headings(self):
        return V.tool_result(V.check_competing(self.ctx, self.proposal or {}))

    def t_check_note_coverage(self):
        return V.tool_result(V.check_notes(self.ctx, self.proposal or {}))
