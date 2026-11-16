"""Versioned prompt artifacts (L2). Each version is a directory: meta.json + one .md file per role."""
import hashlib
import json
import os
from dataclasses import dataclass

_DIR = os.path.dirname(__file__)


@dataclass
class PromptSet:
    version: str
    texts: dict
    flags: dict
    sha: str
    description: str


_cache: dict[str, PromptSet] = {}


def load_prompts(version: str) -> PromptSet:
    if version in _cache:
        return _cache[version]
    d = os.path.join(_DIR, version)
    if not os.path.isdir(d):
        raise ValueError(f"unknown prompt version {version!r}")
    meta = json.load(open(os.path.join(d, "meta.json")))
    texts, h = {}, hashlib.sha1()
    for role in ("extract", "research", "verify"):
        t = open(os.path.join(d, f"{role}.md"), encoding="utf-8").read().strip()
        flags = json.dumps({"behaviour_flags": meta["flags"]})
        texts[role] = f"ROLE: {role}\nPROMPT_VERSION: {version}\nBEHAVIOUR_FLAGS: {flags}\n\n{t}"
        h.update(texts[role].encode())
    ps = PromptSet(version, texts, meta["flags"], h.hexdigest()[:10], meta.get("description", ""))
    _cache[version] = ps
    return ps


def available_versions() -> list[str]:
    return sorted(x for x in os.listdir(_DIR) if os.path.isdir(os.path.join(_DIR, x)) and x.startswith("v"))
