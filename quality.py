"""Quality metrics (L3, L6). Computed locally, no LLM judge, so they are cheap enough to score live traffic and to run in CI.

groundedness      share of rationale sentences whose content stems are >=60% covered by the cited passages (plus the request text)
context_relevance mean cosine similarity between the request and every retrieved passage (local embedder)
faithfulness      share of citations that resolve, quote the passage verbatim, and (for heading citations) agree with the proposed code
hallucination     a run hallucinates if any citation is unresolved/misquoted or the proposed code is not a heading in the corpus
These are lexical/embedding proxies for TruLens-style feedback functions; see docs/EVALUATION.md for their limits."""
from __future__ import annotations

import re

import numpy as np

from app.corpus import heuristics as H
from app.corpus.embeddings import get_embedder
from app.textutil import norm, stems


def _sentences(t: str) -> list[str]:
    return [x.strip() for x in re.split(r"(?<=[.;])\s+", t or "") if len(x.strip()) > 12]


def _cstems(t: str) -> set[str]:
    return set(H.content_stems(t))


def score_result(request: dict, result: dict, retrieved: dict[str, dict], store) -> dict:
    prop = result.get("proposal") or {}
    cites = (prop.get("citations") or []) + ([prop["export_policy_citation"]] if prop.get("export_policy_citation") else [])
    out = {"groundedness": None, "context_relevance": None, "faithfulness": None, "hallucinated": False}
    if retrieved:
        emb = get_embedder()
        q = emb.embed([request.get("product_description", "")])[0]
        ps = emb.embed([p.get("embed_text") or p["text"] for p in retrieved.values()])
        out["context_relevance"] = round(float(np.mean(ps @ q)), 4)
    if not prop or not cites:
        return out
    support = set(stems(request.get("product_description", "")))
    ok, bad_code = 0, False
    for c in cites:
        p = retrieved.get(c["passage_id"]) or store.by_id.get(c["passage_id"])
        if not p:
            out["hallucinated"] = True
            continue
        support |= set(stems(p["text"] + " " + (p.get("clause") or "") + " " + (p.get("heading_title") or "")))
        if norm(c.get("quote", "")) and norm(c["quote"]) in norm(p["text"]):
            ok += 1
        else:
            out["hallucinated"] = True
    code = prop.get("heading_code")
    if code:
        if not any((store.by_id.get(c["passage_id"]) or {}).get("heading_code") == code for c in cites):
            bad_code = True
        if not any(h.get("heading_code") == code for h in store.headings()):
            out["hallucinated"] = True
    out["faithfulness"] = round((ok / len(cites)) * (0.0 if bad_code else 1.0), 3)
    sents = _sentences(prop.get("rationale", ""))
    if sents:
        g = []
        for s in sents:
            cs = _cstems(s)
            g.append(1.0 if not cs else (1.0 if len(cs & support) / len(cs) >= 0.6 else 0.0))
        out["groundedness"] = round(sum(g) / len(g), 3)
    return out


def mean(vals):
    v = [x for x in vals if x is not None]
    return round(sum(v) / len(v), 4) if v else None
