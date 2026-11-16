import re
import unicodedata

_TOKEN = re.compile(r"[a-z0-9]+")


def stem(w: str) -> str:
    """Very small suffix stemmer: enough to match flask/flasks, pen/pens, drilled/drill."""
    if len(w) <= 3:
        return w
    for suf in ("ings", "ing", "ies", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            base = w[: -len(suf)]
            w = base + "y" if suf == "ies" else base
            break
    if w.endswith("e") and len(w) > 4:  # sieve/sieves, machine/machines
        w = w[:-1]
    return w


def tokens(text: str) -> list[str]:
    return _TOKEN.findall(unicodedata.normalize("NFKD", text or "").lower())


def stems(text: str) -> list[str]:
    return [stem(t) for t in tokens(text)]


def norm(text: str) -> str:
    return " ".join(tokens(text))
