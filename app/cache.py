"""Cache + shared small state. Redis when REDIS_URL is set, otherwise an in-process dict (tests/dev)."""
import json
import threading
import time
from typing import Any

from app.config import get_settings


class _MemoryBackend:
    def __init__(self):
        self._d: dict[str, tuple[float | None, str]] = {}
        self._lock = threading.Lock()

    def get(self, k):
        with self._lock:
            v = self._d.get(k)
            if not v:
                return None
            exp, val = v
            if exp is not None and exp < time.time():
                self._d.pop(k, None)
                return None
            return val

    def set(self, k, v, ttl=None):
        with self._lock:
            self._d[k] = (time.time() + ttl if ttl else None, v)

    def delete(self, k):
        with self._lock:
            self._d.pop(k, None)

    def incr_window(self, k, window_s) -> int:
        with self._lock:
            now = time.time()
            exp, val = self._d.get(k, (now + window_s, "0"))
            if exp < now:
                exp, val = now + window_s, "0"
            n = int(val) + 1
            self._d[k] = (exp, str(n))
            return n

    def ttl(self, k) -> int:
        v = self._d.get(k)
        return max(0, int(v[0] - time.time())) if v and v[0] else 0

    def ping(self):
        return True

    def clear(self):
        with self._lock:
            self._d.clear()


class _RedisBackend:
    def __init__(self, url: str):
        import redis

        self.r = redis.Redis.from_url(url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2)

    def get(self, k):
        return self.r.get(k)

    def set(self, k, v, ttl=None):
        self.r.set(k, v, ex=ttl)

    def delete(self, k):
        self.r.delete(k)

    def incr_window(self, k, window_s) -> int:
        p = self.r.pipeline()
        p.incr(k)
        p.expire(k, window_s, nx=True)
        return int(p.execute()[0])

    def ttl(self, k) -> int:
        return max(0, int(self.r.ttl(k)))

    def ping(self):
        return self.r.ping()

    def clear(self):
        self.r.flushdb()


_backend = None
_lock = threading.Lock()


def backend():
    global _backend
    with _lock:
        if _backend is None:
            url = get_settings().redis_url
            _backend = _RedisBackend(url) if url else _MemoryBackend()
        return _backend


def reset_backend() -> None:
    global _backend
    _backend = None


def cache_get_json(key: str) -> Any | None:
    try:
        v = backend().get(key)
    except Exception:
        return None
    return json.loads(v) if v else None


def cache_set_json(key: str, value: Any, ttl: int | None = None) -> None:
    try:
        backend().set(key, json.dumps(value, default=str), ttl or get_settings().cache_ttl_s)
    except Exception:
        pass
