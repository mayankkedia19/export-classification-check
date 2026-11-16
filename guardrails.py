"""Prompt-injection defence, toxicity detection and output filtering (L4)."""
import re
import unicodedata

from app.config import get_settings


class GuardrailViolation(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


_INJECTION = [
    r"ignore (all |any |the )?(previous|prior|above|earlier) (instructions|prompts?|rules)",
    r"disregard (all |any |the )?(previous|prior|above|earlier|your) (instructions|prompts?|rules)",
    r"(reveal|show|print|repeat|leak) (me )?(your |the )?(system|hidden|initial) (prompt|instructions|message)",
    r"you are now\b",
    r"\bnew (instructions|rules|persona)\b",
    r"\b(act|behave|respond) as (if|an?|the)\b.{0,40}(admin|root|developer|unrestricted|dan)\b",
    r"\bjailbreak\b",
    r"\bdeveloper mode\b",
    r"\bsystem prompt\b",
    r"override (the )?(verification|verifier|guardrails?|rules|policy|route)",
    r"(mark|set|route|classify)\b.{0,30}\b(as )?clear[- ]for[- ]filing\b.{0,30}(regardless|anyway|always|no matter)",
    r"</?\s*(untrusted_input|system|assistant|state|tool|instructions?)\s*>",
    r"<\|.{0,20}\|>",
    r"\b(file|submit) (the |a )?(customs )?declaration\b",
    r"(call|invoke|use) (the )?tool\b.{0,40}(delete|drop|exfiltrate|send)",
    r"\bbase64\b.{0,30}\b(decode|execute)\b",
]
_INJ_RE = [re.compile(p, re.I | re.S) for p in _INJECTION]

_TOXIC = [
    r"\b(fuck|shit|bitch|bastard|asshole|cunt)\w*\b",
    r"\b(kill|murder) (you|them|him|her|yourself)\b",
    r"\b(idiot|moron|stupid) (bot|ai|machine|system)\b",
    r"\bkys\b",
]
_TOX_RE = [re.compile(p, re.I) for p in _TOXIC]

_FORBIDDEN_OUTPUT = [
    r"\b(i|we) (have |'ve )?(filed|submitted|lodged)\b",
    r"\bdeclaration (has been|was|is) (filed|submitted|lodged)\b",
    r"\blegally (binding|final)\b",
    r"\bofficially (approved|cleared|certified)\b",
    r"\bapproved for (customs|export)\b",
    r"\bguarantee[sd]?\b.{0,20}\b(clearance|duty|no penalty)\b",
    r"\bduty (rate )?(of|is|will be) \d",
]
_FORB_RE = [re.compile(p, re.I) for p in _FORBIDDEN_OUTPUT]

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁠﻿]")


def normalise(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "")
    return _CTRL.sub("", t)


def detect_injection(text: str) -> list[str]:
    t = normalise(text)
    return [p.pattern for p in _INJ_RE if p.search(t)]


def detect_toxicity(text: str) -> list[str]:
    t = normalise(text)
    return [p.pattern for p in _TOX_RE if p.search(t)]


def neutralise(text: str, max_len: int = 1500) -> str:
    """Make untrusted text inert before it is placed in a model prompt: strip control chars,
    break any delimiter tags, collapse whitespace and truncate. Content stays data, never instructions."""
    t = normalise(text)
    t = re.sub(r"<\s*/?\s*([a-zA-Z_]+)\s*>", r"[\1]", t)
    t = t.replace("```", "'''")
    t = re.sub(r"\s+", " ", t).strip()
    return t[:max_len]


def check_input_text(fields: dict[str, str | None]) -> list[tuple[str, str, str]]:
    """Returns list of (field, code, message) for guardrail hits across free-text fields."""
    hits = []
    for name, val in fields.items():
        if not val:
            continue
        if detect_injection(val):
            hits.append((name, "guardrail.prompt_injection", "possible prompt-injection content detected"))
        if detect_toxicity(val):
            hits.append((name, "guardrail.toxicity", "toxic language detected"))
    return hits


def assert_safe_for_model(text: str, where: str) -> None:
    """Defence in depth at the model boundary (covers agent-derived text as well as user text)."""
    if detect_injection(text):
        raise GuardrailViolation("guardrail.prompt_injection", f"injection pattern reached model boundary at {where}")


def filter_output_text(text: str) -> tuple[str, list[str]]:
    """Output filter: the service may not issue determinations it is not authorised to make."""
    hits = [p.pattern for p in _FORB_RE if p.search(text or "")]
    if hits or detect_toxicity(text or ""):
        return "[withheld by output filter: contained language the service is not authorised to issue]", hits or ["toxicity"]
    return text, []


def max_prompt_chars() -> int:
    return 60_000 if get_settings().app_env != "dev" else 120_000
