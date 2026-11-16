"""LangGraph orchestration of the two agents.

   START -> research(Agent 1) -> handoff -> verify(Agent 2) -> route -> END
                 ^                              |
                 +------- retry(new strategy) <-+   (verification failed, attempts and time remain)
   research --provider down--> degrade -> route          research --limit/timeout/guardrail--> route (escalate)

Failure semantics (A2): Agent 1 fails -> deterministic retrieval-only fallback, specialist review. Agent 2 fails ->
deterministic checks still gate the answer, route can never be clear-for-filing."""
from __future__ import annotations

import logging
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from opentelemetry import trace

from app.agents import verification as V
from app.agents.context import RunContext, RunTimeout, StepLimitExceeded, ToolBudgetExceeded
from app.agents.fallback import run_fallback
from app.agents.research import STRATEGIES, run_research
from app.agents.routing import decide_route
from app.agents.verifier import run_verifier
from app.corpus.store import corpus_mode
from app.guardrails import GuardrailViolation, filter_output_text
from app.llm.base import LLMFormatError, ProviderUnavailable
from app.logging_setup import log
from app.metrics import AGENT_TASKS, AGENT_STEPS, AGENT_TOOLCALLS, DEGRADED, GUARDRAIL_HITS, VERIFY_OUTCOMES
from app.schemas import Proposal

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("ecc.graph")


class GState(TypedDict, total=False):
    attempt: int
    strategy: str
    feedback: list
    proposal: dict
    verification: dict
    history: list
    tried: list
    degraded: bool
    degrade_reason: str
    verifier_degraded: bool
    failure: str
    next_hint: str | None
    result: dict


def _ctx(config) -> RunContext:
    return config["configurable"]["ctx"]


def n_research(state: GState, config) -> GState:
    ctx = _ctx(config)
    strategy, attempt = state["strategy"], state["attempt"]
    with tracer.start_as_current_span("agent.research") as sp:
        sp.set_attribute("strategy", strategy)
        sp.set_attribute("attempt", attempt)
        try:
            prop = run_research(ctx, strategy, attempt, state.get("feedback", []))
            AGENT_TASKS.labels("research", "success").inc()
            return {"proposal": prop, "tried": state.get("tried", []) + [strategy]}
        except ProviderUnavailable as e:
            AGENT_TASKS.labels("research", "provider_unavailable").inc()
            DEGRADED.labels("research_provider_unavailable").inc()
            log(logger, logging.ERROR, "research agent degraded", reason=str(e))
            prop = run_fallback(ctx, str(e))
            ctx.record("research", "system", "degrade", {"reason": str(e)}, {"mode": "deterministic_retrieval_only"})
            return {"proposal": prop, "degraded": True, "degrade_reason": str(e)}
        except GuardrailViolation as e:
            AGENT_TASKS.labels("research", "guardrail").inc()
            return {"failure": f"guardrail: {e.code}", "proposal": Proposal(status="incomplete", rationale="blocked by guardrail").model_dump()}
        except (RunTimeout, StepLimitExceeded, ToolBudgetExceeded, LLMFormatError) as e:
            AGENT_TASKS.labels("research", type(e).__name__).inc()
            ctx.record("research", "system", "limit", {}, {"error": str(e)}, status="error", error=type(e).__name__)
            return {"failure": f"{type(e).__name__}: {e}", "proposal": Proposal(status="incomplete", rationale="research did not finish within limits").model_dump()}


def n_handoff(state: GState, config) -> GState:
    """Explicit hand-off artifact between agents: the proposal and its citation ids, nothing of Agent 1's reasoning."""
    ctx = _ctx(config)
    p = state["proposal"]
    ctx.record("research->verifier", "handoff", "proposal", None,
               {"status": p.get("status"), "heading_code": p.get("heading_code"), "citations": [c["passage_id"] for c in p.get("citations", [])],
                "attempt": state["attempt"], "strategy": state["strategy"]})
    return {}


def n_verify(state: GState, config) -> GState:
    ctx = _ctx(config)
    prop = dict(state["proposal"])
    agent_res, vdeg = {"verdict": "pass", "issues": [], "next_strategy": None}, False
    if not ctx.s.verify_enabled:  # ablation only
        ver = {"passed": True, "checks": [], "issues": [], "attempts": state["attempt"], "history": [{"attempt": state["attempt"], "strategy": state["strategy"], "passed": True, "issues": ["verification disabled (ablation)"]}]}
        prop["confidence"] = V.compute_confidence(ctx, prop, ver) if prop.get("status") == "proposed" else 0.0
        ctx.record("verifier", "verify", "gate", {}, {"passed": True, "disabled": True})
        return {"verification": ver, "proposal": prop, "history": ver["history"], "verifier_degraded": False, "next_hint": None}
    with tracer.start_as_current_span("agent.verifier") as sp:
        try:
            agent_res = run_verifier(ctx, prop)
            AGENT_TASKS.labels("verifier", "success").inc()
        except (ProviderUnavailable, RunTimeout, StepLimitExceeded, ToolBudgetExceeded, LLMFormatError, GuardrailViolation) as e:
            vdeg = True
            AGENT_TASKS.labels("verifier", type(e).__name__).inc()
            DEGRADED.labels("verifier_unavailable").inc()
            ctx.record("verifier", "system", "degrade", {}, {"mode": "deterministic_checks_only", "error": str(e)[:200]}, status="error", error=type(e).__name__)
        det = V.verify_proposal(ctx, prop)  # authoritative gate, re-run in code after the agent loop
        issues = list(det["issues"]) + [i for i in agent_res["issues"] if i not in det["issues"]]
        passed = det["passed"] and agent_res["verdict"] == "pass"
        sp.set_attribute("passed", passed)
    attempt = state["attempt"]
    ver = {"passed": passed, "checks": det["checks"], "issues": issues, "attempts": attempt,
           "history": state.get("history", []) + [{"attempt": attempt, "strategy": state["strategy"], "passed": passed, "issues": issues,
                                                  "proposal_heading": prop.get("heading_code"), "proposal_status": prop.get("status")}]}
    VERIFY_OUTCOMES.labels("pass" if passed else "fail").inc()
    prop["confidence"] = V.compute_confidence(ctx, prop, ver)
    ctx.record("verifier", "verify", "gate", {"attempt": attempt}, {"passed": passed, "issues": issues, "confidence": prop["confidence"]})
    return {"verification": ver, "proposal": prop, "history": ver["history"], "verifier_degraded": vdeg, "next_hint": agent_res.get("next_strategy")}


def after_research(state: GState) -> str:
    if state.get("degraded"):
        return "degrade"
    return "route" if state.get("failure") else "handoff"


def _next_strategy(state: GState) -> str | None:
    tried = state.get("tried", [])
    hint = state.get("next_hint")
    if hint in STRATEGIES and hint not in tried:
        return hint
    return next((s for s in STRATEGIES if s not in tried), None)


def after_verify(state: GState, config) -> str:
    ctx = _ctx(config)
    if state["verification"]["passed"] or state.get("verifier_degraded"):
        return "route"
    if state["attempt"] >= ctx.s.max_research_attempts or ctx.remaining() < 8 or not _next_strategy(state):
        return "route"
    return "retry"


def n_retry(state: GState, config) -> GState:
    ctx = _ctx(config)
    nxt = _next_strategy(state)
    ctx.record("graph", "system", "retry", {"issues": state["verification"]["issues"]}, {"next_strategy": nxt, "attempt": state["attempt"] + 1})
    return {"attempt": state["attempt"] + 1, "strategy": nxt, "feedback": state["verification"]["issues"]}


def n_degrade(state: GState, config) -> GState:
    return {}


def _citation_models(ctx, cites):
    return cites


def n_route(state: GState, config) -> GState:
    ctx = _ctx(config)
    prop = state.get("proposal") or Proposal(status="incomplete").model_dump()
    ver = state.get("verification") or {"passed": False, "checks": [], "issues": [state.get("failure", "no verification")], "attempts": state.get("attempt", 1), "history": state.get("history", [])}
    degraded = bool(state.get("degraded"))
    route, basis, esc = decide_route(ctx, prop, ver, degraded=degraded, verifier_degraded=bool(state.get("verifier_degraded")), failure=state.get("failure"))
    withheld = not ver["passed"] and not degraded
    out_prop = None if (withheld or (state.get("failure") and not prop.get("candidates"))) else prop
    if out_prop is not None and degraded:
        out_prop = {**prop, "heading_code": None, "confidence": 0.0}
    warnings = []
    mode = corpus_mode(list(ctx.store.chunks))
    if mode != "official":
        warnings.append(f"Evidence comes from a {mode.upper()} corpus: approximate paraphrase, NOT official legal text. Verify against the CBIC Customs Tariff and DGFT ITC(HS) before any use.")
    if degraded:
        warnings.append("Model provider was unavailable: deterministic retrieval only, no determination made.")
    if state.get("verifier_degraded"):
        warnings.append("Second-agent verification was unavailable; only deterministic checks were applied.")
    if out_prop:
        txt, hits = filter_output_text(out_prop.get("rationale", ""))
        if hits:
            GUARDRAIL_HITS.labels("output_filter").inc()
            out_prop["rationale"] = txt
            warnings.append("Output filter withheld part of the explanation.")
    basis, bhits = filter_output_text(basis)
    if bhits:
        GUARDRAIL_HITS.labels("output_filter").inc()
    completeness = "incomplete" if state.get("failure") else ("degraded" if (degraded or state.get("verifier_degraded")) else "complete")
    pol = out_prop.get("export_policy_citation") if out_prop else None
    result = {
        "status": "completed", "completeness": completeness, "route": route.value, "route_basis": basis, "escalation_reason": esc,
        "proposal": out_prop, "entities": ctx.entities, "verification": ver, "corpus_mode": mode,
        "corpus_version": ctx.store.info.get("corpus_version", ""), "prompt_version": ctx.prompts.version, "warnings": warnings,
    }
    AGENT_STEPS.labels("research").observe(ctx.loop_steps.get("research", 0))
    AGENT_STEPS.labels("verifier").observe(ctx.loop_steps.get("verifier", 0))
    n_tools_r = sum(1 for s in ctx.steps_log if s["kind"] == "tool" and s["agent"] == "research")
    AGENT_TOOLCALLS.labels("research").observe(n_tools_r)
    AGENT_TOOLCALLS.labels("verifier").observe(ctx.tool_calls - n_tools_r)
    ctx.record("router", "route", "decision", {"status": prop.get("status")}, {"route": route.value, "basis": basis})
    return {"result": result}


def build_graph():
    g = StateGraph(GState)
    g.add_node("research", n_research)
    g.add_node("handoff", n_handoff)
    g.add_node("verify", n_verify)
    g.add_node("retry", n_retry)
    g.add_node("degrade", n_degrade)
    g.add_node("route", n_route)
    g.add_edge(START, "research")
    g.add_conditional_edges("research", after_research, {"degrade": "degrade", "handoff": "handoff", "route": "route"})
    g.add_edge("handoff", "verify")
    g.add_conditional_edges("verify", after_verify, {"retry": "retry", "route": "route"})
    g.add_edge("retry", "research")
    g.add_edge("degrade", "route")
    g.add_edge("route", END)
    return g.compile()


_graph = None


def run_graph(ctx: RunContext) -> dict:
    global _graph
    if _graph is None:
        _graph = build_graph()
    from app.agents.research import STRATEGIES as S

    first = ctx.prompts.flags.get("default_strategy", S[0])
    init: GState = {"attempt": 1, "strategy": first if first in S else S[0], "feedback": [], "history": [], "tried": []}
    final = _graph.invoke(init, config={"configurable": {"ctx": ctx}, "recursion_limit": 40})
    return final["result"]
