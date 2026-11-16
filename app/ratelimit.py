from app.cache import backend
from app.config import get_settings


def check_rate_limit(client_id: str) -> tuple[bool, int, int]:
    """Fixed-window per-client limiter. Returns (allowed, limit, retry_after_s).

    Rate limiting is a cost control: each accepted request triggers a multi-step agent run."""
    limit = get_settings().rate_limit_per_min
    if limit <= 0:
        return True, limit, 0
    key = f"rl:{client_id}"
    try:
        n = backend().incr_window(key, 60)
        if n > limit:
            return False, limit, max(1, backend().ttl(key))
    except Exception:
        return True, limit, 0  # fail open on limiter outage; budget still bounded by run caps
    return True, limit, 0
