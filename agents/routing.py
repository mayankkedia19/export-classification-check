"""Routing: one of clear-for-filing | seek-product-clarification | specialist-classification-review (F6, F7)."""
from __future__ import annotations

from app.agents import verification as V
from app.schemas import Route


def decide_route(ctx, proposal: dict, verification: dict, *, degraded: bool, verifier_degraded: bool, failure: str | None):
    """Returns (route, basis, escalation_reason|None). An unverified or incomplete run is never routed as clear."""
    st = proposal.get("status")
    if failure:
        return Route.SPECIALIST, f"Run could not be completed safely: {failure}.", failure
    if degraded:
        return Route.SPECIALIST, "Automated analysis was unavailable; candidate headings are listed for a specialist, with no determination made.", "degraded: model provider unavailable"
    if not verification["passed"]:
        iss = "; ".join(verification["issues"][:4])
        return Route.SPECIALIST, f"Proposal failed verification after {verification.get('attempts', 1)} attempt(s) and is withheld: {iss}.", f"verification failed: {iss}"
    if st == "out_of_scope":
        return Route.SPECIALIST, "The product falls outside the chapters this service covers (73 and 96).", "out of scope: " + (proposal.get("rationale") or "")[:200]
    if st == "tie":
        codes = ", ".join(c["heading_code"] for c in proposal.get("candidates", []))
        return Route.SPECIALIST, f"Headings {codes} remain equally supported after GRI 3; the service does not choose between them.", f"tie after GRI 3: {codes}"
    if st == "insufficient_information":
        miss = ", ".join(proposal.get("missing_information") or [])
        return Route.CLARIFY, f"The classification depends on information the request does not give: {miss}.", None
    if st == "proposed":
        hp = next((ctx.retrieved.get(c["passage_id"]) for c in proposal["citations"] if (ctx.retrieved.get(c["passage_id"]) or {}).get("doc_type") == "heading"), None)
        if hp and hp["heading_title"].lower().startswith("other articles"):
            return Route.SPECIALIST, "Only the residual heading matched; articles may be named in chapters outside this service's corpus.", "residual heading only"
        conf = proposal.get("confidence", 0.0)
        if verifier_degraded:
            return Route.CLARIFY if conf >= V.CONF_CLEAR else Route.SPECIALIST, "Second-agent verification was unavailable; deterministic checks passed but the result is not cleared automatically.", None
        if conf >= V.CONF_CLEAR:
            return Route.CLEAR, f"Heading {proposal['heading_code']} was proposed by GRI {proposal.get('gri_applied')} and every citation was verified against retrieved text (confidence {conf}).", None
        return Route.CLARIFY, f"Evidence supports heading {proposal['heading_code']} only weakly (confidence {conf}); please confirm product details.", None
    return Route.SPECIALIST, "No supported proposal.", "no supported proposal"
