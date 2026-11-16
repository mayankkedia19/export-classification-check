"""Deterministic stand-in for the LLM (D6). It is a *policy*, not a lookup table: it reads the structured state in the
prompt (request text + the tool observations so far) and returns the next JSON action. It contains no heading codes;
every code it proposes comes from a passage returned by a tool in the same conversation.

Fault injection (admin /admin/chaos) lets the demo show verification and the circuit breaker working."""
from __future__ import annotations

import json
import re

from app.cache import cache_get_json, cache_set_json
from app.corpus import heuristics as H


def _parse(messages):
    sys_ = messages[0]["content"]
    user = messages[-1]["content"]
    role = re.search(r"ROLE: (\w+)", sys_).group(1)
    flags = json.loads(re.search(r"BEHAVIOUR_FLAGS: (\{.*\})", sys_).group(1))["behaviour_flags"]
    req = json.loads(re.search(r"<untrusted_input>(.*?)</untrusted_input>", user, re.S).group(1))
    state = json.loads(re.search(r"<state>(.*)</state>", user, re.S).group(1))
    return role, flags, req, state


def _take_fault(name: str) -> bool:
    f = cache_get_json("chaos:llm") or {}
    if f.get("mode") != name:
        return False
    n = int(f.get("count", 1))
    if n <= 0:
        return False
    f["count"] = n - 1
    cache_set_json("chaos:llm", f, 3600)
    return True


def respond(messages, consume_fault: bool = True) -> tuple[str, int, int]:
    role, flags, req, state = _parse(messages)
    if consume_fault and _take_fault("malformed_json"):
        out = "Sure! Here is my answer: {not valid json"
    elif role == "extract":
        out = json.dumps(_extract(req))
    elif role == "research":
        out = json.dumps(_research(req, state, flags, fabricate=consume_fault and _peek_fabricate()))
    elif role == "verify":
        out = json.dumps(_verify(state))
    else:
        out = json.dumps({"error": "unknown role"})
    pt = sum(len(m["content"]) for m in messages) // 4
    return out, pt, len(out) // 4


def _peek_fabricate() -> bool:
    return (cache_get_json("chaos:llm") or {}).get("mode") == "fabricate_citation"


def _extract(req: dict) -> dict:
    desc = req.get("product_description", "")
    mats = H.detect_materials(desc, req.get("materials"))
    article = H.extract_article(desc, mats)
    missing = [] if article else ["article"]
    return {"article": article, "materials": mats, "function": req.get("intended_use"),
            "attributes": {"capacity_litres": req.get("capacity_litres")}, "missing": missing}


def _quote(p: dict, n: int = 190) -> str:
    t = p["text"]
    if len(t) <= n:
        return t
    cut = t[:n].rsplit(" ", 1)[0]
    return cut


def _cite(p: dict) -> dict:
    return {"passage_id": p["passage_id"], "quote": _quote(p)}


def _research(req, state, flags, fabricate=False):
    hist = state.get("history", [])
    strategy = state.get("strategy", flags.get("default_strategy", "article_first"))
    attempt = state.get("attempt", 1)
    feedback = state.get("feedback", [])
    ent = state.get("entities", {})
    desc = req.get("product_description", "")
    art = ent.get("article") or desc
    seen: dict[str, dict] = {}
    for h in hist:
        for p in h.get("passages", []):
            seen[p["passage_id"]] = p

    def done(tool, **match):
        return any(h["tool"] == tool and all(h["args"].get(k) == v for k, v in match.items()) for h in hist)

    headings = [p for p in seen.values() if p["doc_type"] == "heading" and "name_score" in p]
    named = sorted([p for p in headings if p["name_score"] >= 0.9 and not p.get("carved_out_by")],
                   key=lambda p: (-p["name_score"], p["heading_code"]))
    cap = req.get("capacity_litres")

    def viable(p):
        c = p.get("capacity_condition")
        if not c or cap is None:
            return True
        return cap <= c[1] if c[0] == "le" else cap > c[1]

    vnamed = [p for p in named if viable(p)]
    top = [p for p in vnamed if abs(p["name_score"] - vnamed[0]["name_score"]) < 1e-6] if vnamed else []
    mat_ch = ent.get("material_chapters", [])

    def tool(name, args, thought):
        return {"action": "tool", "tool": name, "args": args, "thought": thought}

    # 1. discovery according to the strategy
    if strategy == "article_first":
        if not done("search_heading_names"):
            return tool("search_heading_names", {"text": desc}, "GRI 1: look for a heading whose terms name the article before looking at material")
        if not done("search_corpus"):
            return tool("search_corpus", {"query": desc, "k": 6}, "broad search for competing headings and notes")
    elif strategy == "semantic_expand":
        if not done("search_corpus"):
            return tool("search_corpus", {"query": desc, "k": 8}, "retrieve passages resembling the request")
        if not vnamed and not done("search_heading_names"):
            return tool("search_heading_names", {"text": desc}, "no retrieved heading names the article; search heading terms directly")
    else:  # lexical_broad
        if not done("search_corpus"):
            return tool("search_corpus", {"query": art, "k": 6, "mode": "lexical"}, "keyword search on the article name only")
        if not done("search_heading_names"):
            return tool("search_heading_names", {"text": art}, "check heading terms against the article name")

    # 2. notes for the candidate chapter
    cand_ch = (top[0]["chapter"] if top else (named[0]["chapter"] if named else None)) or (mat_ch[0] if mat_ch else None)
    if cand_ch and not done("list_applicable_notes", chapter=cand_ch):
        return tool("list_applicable_notes", {"chapter": cand_ch}, f"read the section/chapter notes that apply to Chapter {cand_ch}")

    # 3. follow-up search derived from what was read: does another provision modify the choice?
    needs_followup = flags.get("derive_followup_query") or attempt > 1 or any(f.startswith("missing_exclusion_note") for f in feedback)
    if needs_followup and top and mat_ch and top[0]["chapter"] not in mat_ch:
        q = f"Chapter {mat_ch[0]} section note does not cover articles of Chapter {top[0]['chapter']}"
        if not done("search_corpus", query=q):
            return tool("search_corpus", {"query": q, "k": 4, "chapter": mat_ch[0]},
                        f"heading {top[0]['heading_code']} is outside the material chapter {mat_ch[0]}; look for a note that excludes it from there")

    # 4. the rules
    if not done("lookup_clause", ref="GRI 1"):
        return tool("lookup_clause", {"ref": "GRI 1"}, "GRI 1 is applied first")
    tie = len(top) >= 2
    cap_ambiguous = tie and cap is None and all(p.get("capacity_condition") for p in top)
    if tie and not cap_ambiguous:
        for r in ("GRI 3(a)", "GRI 3(b)", "GRI 3(c)"):
            if not done("lookup_clause", ref=r):
                return tool("lookup_clause", {"ref": r}, "two headings remain; apply GRI 3 in order")

    # 5. decide
    gri1 = next((p for p in seen.values() if p.get("clause") == "GRI 1"), None)
    gri = {p["clause"]: p for p in seen.values() if p["doc_type"] == "gri"}

    def final(prop):
        return {"action": "final", "proposal": prop}

    def policy_for(ch):
        return next((p for p in seen.values() if p["doc_type"] == "export_policy" and ch in (p.get("applies_to_chapters") or [p.get("chapter")])), None)

    if vnamed and not tie:
        h = vnamed[0]
        if not policy_for(h["chapter"]) and not done("search_corpus", doc_type="export_policy"):
            return tool("search_corpus", {"query": f"export policy Chapter {h['chapter']}", "doc_type": "export_policy", "chapter": h["chapter"], "k": 1},
                        "retrieve the export policy for the chosen chapter")
        cites = ([_cite(gri1)] if gri1 else []) + [_cite(h)]
        excl = [p for p in seen.values() if p["doc_type"] in ("section_note", "chapter_note") and h["chapter"] in (p.get("excludes_chapters") or [])
                and set(mat_ch) & set(p.get("applies_to_chapters") or [])]
        cites += [_cite(p) for p in excl[:1]]
        why = f"GRI 1: heading {h['heading_code']} names the article in its own terms ({' '.join(h.get('matched_terms') or [])}); "
        if excl:
            why += f"{excl[0]['clause']} removes articles of Chapter {h['chapter']} from the material chapter, so classification by material does not apply. "
        elif mat_ch and h["chapter"] not in mat_ch:
            why += "no note excluding the material chapter was retrieved. "
        prop = {"status": "proposed", "heading_code": h["heading_code"], "heading_title": h["heading_title"], "gri_applied": "1",
                "rationale": why.strip(), "citations": cites}
        pp = policy_for(h["chapter"])
        if pp:
            prop["export_policy"] = "see cited export policy passage"
            prop["export_policy_citation"] = _cite(pp)
        if fabricate and _take_fault("fabricate_citation"):
            prop["citations"][-1] = {**prop["citations"][-1], "passage_id": prop["citations"][-1]["passage_id"] + ":x9"}
        return final(prop)
    if cap_ambiguous:
        cites = ([_cite(gri1)] if gri1 else []) + [_cite(p) for p in top[:2]]
        return final({"status": "insufficient_information", "citations": cites, "missing_information": ["capacity_litres"],
                      "candidates": [{"heading_code": p["heading_code"], "heading_title": p["heading_title"], "name_score": p["name_score"]} for p in top[:2]],
                      "rationale": "The candidate headings differ by capacity, which the request does not state."})
    if tie:
        pairs = vnamed[:3]
        cites = ([_cite(gri1)] if gri1 else []) + [_cite(p) for p in pairs] + [_cite(gri[c]) for c in ("GRI 3(a)", "GRI 3(b)", "GRI 3(c)") if c in gri]
        return final({"status": "tie", "gri_applied": "3", "citations": cites,
                      "candidates": [{"heading_code": p["heading_code"], "heading_title": p["heading_title"], "name_score": p["name_score"]} for p in pairs],
                      "rationale": "Each candidate heading names part of the article equally (GRI 3(a) cannot prefer one), and the description does not allow an essential character to be determined under GRI 3(b). The tie is not broken automatically."})
    if set(ent.get("materials", [])) & H.IRON_STEEL:
        RQ = "other articles of iron or steel residual heading"
        res = [p for p in headings if p.get("residual")]
        if not res:
            if not done("search_corpus", query=RQ):
                return tool("search_corpus", {"query": RQ, "k": 3, "chapter": "73"}, "no heading names the article; check the residual heading for the material")
        else:
            h = res[0]
            scope = next((p for p in seen.values() if p.get("clause", "").startswith("Chapter 73, scope")), None)
            if not scope and not done("list_applicable_notes", chapter="73"):
                return tool("list_applicable_notes", {"chapter": "73"}, "read the notes that explain the residual heading")
            pp = policy_for("73")
            if not pp and not done("search_corpus", doc_type="export_policy"):
                return tool("search_corpus", {"query": "export policy Chapter 73", "doc_type": "export_policy", "chapter": "73", "k": 1}, "retrieve the export policy for the chosen chapter")
            cites = ([_cite(gri1)] if gri1 else []) + [_cite(h)] + ([_cite(scope)] if scope else [])
            prop = {"status": "proposed", "heading_code": h["heading_code"], "heading_title": h["heading_title"], "gri_applied": "1", "citations": cites,
                    "rationale": "No heading in Chapters 73 or 96 names the article; only the residual heading for articles of iron or steel remains. Articles named in other chapters are outside this corpus."}
            if pp:
                prop["export_policy_citation"] = _cite(pp)
            return final(prop)
    reason = "no heading in Chapters 73 or 96 names the article and the material is not iron or steel"
    carved = [p for p in headings if p.get("carved_out_by")]
    if carved:
        reason = f"heading {carved[0]['heading_code']} carves this item out ('{carved[0]['carved_out_by']}'); the item belongs outside Chapters 73 and 96"
    return final({"status": "out_of_scope", "rationale": reason, "citations": [_cite(gri1)] if gri1 else []})


def _verify(state):
    done = {t["tool"]: t for t in state.get("tool_results", [])}
    for name in ("verify_citations", "check_competing_headings", "check_note_coverage"):
        if name not in done:
            return {"action": "tool", "tool": name, "args": {}, "thought": f"run {name}"}
    issues = [i for t in done.values() for i in t.get("issues", [])]
    nxt = None
    if any(i.startswith("missing_exclusion_note") for i in issues):
        nxt = "semantic_expand"
    elif issues:
        nxt = "article_first"
    return {"action": "final", "verdict": "fail" if issues else "pass", "issues": issues, "next_strategy": nxt}
