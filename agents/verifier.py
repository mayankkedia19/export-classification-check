"""Agent 2: Verification and Routing Agent. Separate prompt, tool set, loop and state from Agent 1. It receives only the
handed-off proposal and the evidence store, never Agent 1's reasoning, and decides what to check and when to re-search."""
from __future__ import annotations

from app.agents.context import RunContext
from app.agents.tools import ToolBox
from app.agents import verification as V
from app.llm.base import LLMFormatError


def run_verifier(ctx: RunContext, proposal: dict) -> dict:
    tb = ToolBox(ctx, "verifier")
    tb.proposal = proposal
    results: list[dict] = []
    repairs = 0
    summary = {k: proposal.get(k) for k in ("status", "heading_code", "gri_applied", "rationale", "missing_information")}
    summary["citations"] = [{"passage_id": c["passage_id"], "quote": c["quote"][:120]} for c in proposal.get("citations") or []]
    for turn in range(ctx.s.max_agent_steps):
        ctx.bump_loop("verifier")
        state = {"proposal": summary, "tool_results": results, "tools": tb.specs(), "steps_left": ctx.s.max_agent_steps - turn}
        try:
            act = ctx.llm_json("verifier", "verify", "decide", state)
        except LLMFormatError:
            repairs += 1
            if repairs > 1:
                return {"verdict": "fail", "issues": ["verifier_malformed_output"], "next_strategy": None}
            continue
        if act.get("action") == "tool":
            res = tb.call(str(act.get("tool")), act.get("args") if isinstance(act.get("args"), dict) else {})
            results.append({"tool": act.get("tool"), "issues": res.get("issues", []), "checks": res.get("checks", []), "error": res.get("error")})
        elif act.get("action") == "final":
            v = "pass" if act.get("verdict") == "pass" else "fail"
            return {"verdict": v, "issues": [str(i) for i in act.get("issues", [])][:20], "next_strategy": act.get("next_strategy")}
        else:
            repairs += 1
            if repairs > 1:
                return {"verdict": "fail", "issues": ["verifier_unknown_action"], "next_strategy": None}
    return {"verdict": "fail", "issues": ["verifier_step_limit"], "next_strategy": None}
