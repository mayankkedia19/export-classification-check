"""Deterministic verification of a proposal against retrieved text (A3). The verifier agent's tools call these
functions, and the final gate re-runs them in code, so a model can downgrade a result but never upgrade one."""
from __future__ import annotations

from app.corpus import heuristics as H
from app.textutil import norm

CONF_CLEAR = 0.75


def _chk(name: str, passed: bool, detail: str = "", issue: str | None = None) -> dict:
    return {"name": name, "passed": passed, "detail": detail, "issue": None if passed else (issue or name)}


def tool_result(checks: list[dict]) -> dict:
    return {"checks": [{k: c[k] for k in ("name", "passed", "detail")} for c in checks],
            "issues": [c["issue"] for c in checks if not c["passed"]]}


def _all_cites(proposal: dict) -> list[dict]:
    cs = list(proposal.get("citations") or [])
    if proposal.get("export_policy_citation"):
        cs.append(proposal["export_policy_citation"])
    return cs


def _heading_passage(ctx, proposal):
    code = proposal.get("heading_code")
    for c in proposal.get("citations") or []:
        p = ctx.retrieved.get(c["passage_id"])
        if p and p["doc_type"] == "heading" and p.get("heading_code") == code:
            return p
    return None


def check_citations(ctx, proposal: dict) -> list[dict]:
    cites = _all_cites(proposal)
    unresolved = [c["passage_id"] for c in cites if c["passage_id"] not in ctx.retrieved]
    bad_quote = []
    for c in cites:
        p = ctx.retrieved.get(c["passage_id"])
        if p and (not c.get("quote") or norm(c["quote"]) not in norm(p["text"])):
            bad_quote.append(c["passage_id"])
    out = [
        _chk("citations_present", bool(proposal.get("citations")) or proposal.get("status") != "proposed", "a proposal needs citations",
             "no_citations"),
        _chk("citations_resolve", not unresolved, f"unresolved: {unresolved}" if unresolved else f"{len(cites)} citations resolve to retrieved passages",
             f"unresolved_citation: {','.join(unresolved)}"),
        _chk("quotes_verbatim", not bad_quote, f"quote not found in: {bad_quote}" if bad_quote else "all quotes appear verbatim in the cited passage",
             f"quote_not_in_passage: {','.join(bad_quote)}"),
    ]
    return out


def _viable(h: dict, capacity: float | None) -> bool:
    cond = H.capacity_condition(h["heading_title"])
    if not cond or capacity is None:
        return True
    op, lim = cond
    return capacity <= lim if op == "le" else capacity > lim


def scan_headings(ctx) -> list[dict]:
    """Independent scan of every heading in the corpus (not only the ones the agent retrieved)."""
    ds = H.desc_stems(ctx.request.get("product_description", ""))
    rows = []
    for h in ctx.store.headings():
        sc, ph = H.name_match(ds, h["heading_title"])
        carved = H.is_excluded_by_heading(ctx.request.get("product_description", ""), h["heading_title"])
        rows.append({"code": h["heading_code"], "chapter": h["chapter"], "title": h["heading_title"], "score": sc,
                     "carved": bool(carved), "cond": H.capacity_condition(h["heading_title"]), "residual": H.is_residual(h["heading_title"]),
                     "viable": _viable(h, ctx.request.get("capacity_litres"))})
    return rows


def named_candidates(ctx) -> list[dict]:
    return sorted([r for r in scan_headings(ctx) if r["score"] >= 0.9 and not r["carved"]], key=lambda r: (-r["score"], r["code"]))


def check_competing(ctx, proposal: dict) -> list[dict]:
    status, code = proposal.get("status"), proposal.get("heading_code")
    rows = named_candidates(ctx)
    viable = [r for r in rows if r["viable"]]
    out = []
    if status == "proposed" and code:
        mine = next((r for r in scan_headings(ctx) if r["code"] == code), None)
        if mine is None:
            return [_chk("heading_exists", False, f"heading {code} not in corpus", f"unknown_heading: {code}")]
        out.append(_chk("heading_not_carved_out", not mine["carved"], "heading text does not carve the article out" if not mine["carved"] else "heading carves this article out",
                        f"carved_out: {code}"))
        better = [r["code"] for r in viable if r["code"] != code and r["score"] > mine["score"]]
        tied = [r["code"] for r in viable if r["code"] != code and abs(r["score"] - mine["score"]) < 1e-6 and mine["score"] >= 0.9]
        out.append(_chk("no_better_named_heading", not better, f"better-named headings: {better}" if better else "no heading names the article better",
                        f"better_named_heading: {','.join(better)}"))
        out.append(_chk("no_equally_supported_heading", not tied, f"equally supported headings: {tied}" if tied else "no equally supported heading",
                        f"equally_supported_heading: {','.join(tied)}"))
    elif status == "tie":
        ok = len(viable) >= 2 and abs(viable[0]["score"] - viable[1]["score"]) < 1e-6
        out.append(_chk("tie_supported", ok, f"top headings: {[r['code'] for r in viable[:3]]}", "tie_not_supported"))
    elif status == "out_of_scope":
        mats = H.detect_materials(ctx.request.get("product_description", ""), ctx.request.get("materials"))
        ok = not viable and not (set(mats) & H.IRON_STEEL)
        out.append(_chk("out_of_scope_justified", ok, "no heading names the article and no iron/steel material" if ok else
                        f"headings that may apply: {[r['code'] for r in viable[:3]]}", "out_of_scope_not_justified"))
    elif status == "insufficient_information":
        ok = bool(proposal.get("missing_information")) and len([r for r in rows if r["cond"]]) >= 2
        out.append(_chk("clarification_needed", ok, f"missing: {proposal.get('missing_information')}", "clarification_not_supported"))
    return out


def check_notes(ctx, proposal: dict) -> list[dict]:
    if proposal.get("status") != "proposed" or not proposal.get("heading_code"):
        return []
    ch = proposal["heading_code"][:2]
    mat_ch = ctx.entities.get("material_chapters") or []
    out = []
    # Notes that exclude the proposal's own chapter from its own scope
    for p in ctx.retrieved.values():
        if p["doc_type"] in ("section_note", "chapter_note") and ch in (p.get("excludes_chapters") or []) and ch in (p.get("applies_to_chapters") or []):
            out.append(_chk("not_excluded_by_note", False, f"{p['clause']} excludes chapter {ch}", f"excluded_by_note: {p['passage_id']}"))
    if mat_ch and ch not in mat_ch:
        cited = {c["passage_id"] for c in proposal.get("citations") or []}
        ok = any((p := ctx.retrieved.get(pid)) and p["doc_type"] in ("section_note", "chapter_note")
                 and ch in (p.get("excludes_chapters") or []) and set(mat_ch) & set(p.get("applies_to_chapters") or []) for pid in cited)
        out.append(_chk("material_chapter_excluded_by_note", ok,
                        "a cited note removes the article from the material's chapter" if ok else
                        f"article is classified outside the material's chapter {mat_ch}; no retrieved and cited note explains why that chapter does not apply",
                        f"missing_exclusion_note: material chapter {','.join(mat_ch)} vs heading chapter {ch}"))
    elif not out:
        out.append(_chk("not_excluded_by_note", True, "no retrieved note excludes this chapter"))
    return out


def check_core(ctx, proposal: dict) -> list[dict]:
    out = []
    if proposal.get("status") == "incomplete":
        return [_chk("proposal_complete", False, "agent did not finish within its limits", "incomplete_proposal")]
    if proposal.get("status") != "proposed":
        return out
    code = proposal.get("heading_code")
    hp = _heading_passage(ctx, proposal)
    out.append(_chk("heading_cited", hp is not None, f"heading {code} cited" if hp else f"no cited passage is heading {code}", f"heading_not_cited: {code}"))
    if hp:
        ds = H.desc_stems(ctx.request.get("product_description", ""))
        sc, ph = H.name_match(ds, hp["heading_title"])
        residual = H.is_residual(hp["heading_title"])
        ok = sc >= 0.9 or residual
        out.append(_chk("heading_supports_article", ok,
                        f"heading terms {ph} name the article (score {sc})" if sc >= 0.9 else ("residual heading: article not named" if residual else f"heading does not name the article (score {sc})"),
                        f"heading_does_not_name_article: {code}"))
    gri = str(proposal.get("gri_applied") or "")
    cited_clauses = [(ctx.retrieved.get(c["passage_id"]) or {}).get("clause", "") for c in proposal.get("citations") or []]
    out.append(_chk("gri1_cited", any(c.startswith("GRI 1") for c in cited_clauses), "GRI 1 is cited as the first rule applied", "gri1_not_cited"))
    if gri.startswith("3"):
        out.append(_chk("gri_order", any(c.startswith("GRI 3") for c in cited_clauses), "GRI 3 cited after GRI 1", "gri3_not_cited"))
    pc = proposal.get("export_policy_citation")
    pp = ctx.retrieved.get(pc["passage_id"]) if pc else None
    out.append(_chk("export_policy_cited", bool(pp and pp["doc_type"] == "export_policy" and code and code[:2] in (pp.get("applies_to_chapters") or [pp.get("chapter")])),
                    "export policy passage for the chapter is cited" if pp else "no export policy citation", "export_policy_not_cited"))
    return out


def verify_proposal(ctx, proposal: dict) -> dict:
    checks = check_core(ctx, proposal) + check_citations(ctx, proposal) + check_competing(ctx, proposal) + check_notes(ctx, proposal)
    issues = [c["issue"] for c in checks if not c["passed"]]
    return {"passed": not issues, "checks": [{k: c[k] for k in ("name", "passed", "detail")} for c in checks], "issues": issues}


def compute_confidence(ctx, proposal: dict, verification: dict) -> float:
    """Confidence is computed from evidence by the system, never taken from the model."""
    if proposal.get("status") != "proposed" or not verification.get("passed"):
        return 0.0
    hp = _heading_passage(ctx, proposal)
    ds = H.desc_stems(ctx.request.get("product_description", ""))
    name = H.name_match(ds, hp["heading_title"])[0] if hp else 0.0
    cited = [(ctx.retrieved.get(c["passage_id"]) or {}).get("clause", "") for c in proposal["citations"]]
    gri = 1.0 if any(c.startswith("GRI 1") for c in cited) else 0.0
    comp = 1.0 if all(c["passed"] for c in verification["checks"] if "note" in c["name"] or "competing" in c["name"] or "better" in c["name"]) else 0.0
    conf = 0.5 * name + 0.25 * gri + 0.25 * comp
    return round(min(conf, 0.97), 2)
