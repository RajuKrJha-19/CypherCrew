"""A tiny in-process TTL cache - no Redis, no dependency.

Used to stop the dashboard recomputing its most expensive company-wide
aggregates on every page load, when a value a few seconds old is perfectly
fine for an at-a-glance overview. The live counters that must stay current
(build_overview, polled every 10s) are deliberately NOT cached.

Per-worker by design: each gunicorn worker keeps its own small cache, which
is exactly right for cheap, self-refreshing, read-only data - it needs no
shared store and no invalidation, it just goes stale and refreshes.
"""

import functools
import threading
import time


def ttl_cache(seconds):
    """Memoize a function's return value for `seconds`, keyed by its args.

    The cached value is returned as-is, so only use this on functions that
    return plain, immutable-enough data (dicts / lists of dicts) - never
    live ORM instances, which must not outlive their session.
    """

    def decorator(fn):
        store = {}
        lock = threading.Lock()

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            key = (args, tuple(sorted(kwargs.items())))
            now = time.monotonic()

            with lock:
                cached = store.get(key)
                if cached is not None and (now - cached[0]) < seconds:
                    return cached[1]

            # Compute outside the lock so a slow build doesn't block other
            # readers; a brief duplicate compute on a cold key is harmless.
            value = fn(*args, **kwargs)

            with lock:
                store[key] = (now, value)

            return value

        wrapper.cache_clear = store.clear
        return wrapper

    return decorator
