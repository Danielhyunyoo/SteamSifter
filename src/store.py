"""
store.py

Persistence for analyzed games. When REDIS_URL is set (e.g. an Upstash Redis
URL on the deployed site), analyses are saved to Redis so they survive Render
redeploys. With no REDIS_URL (local dev), it falls back to local JSON files.

Only the final analysis is persisted here; the raw reviews/classified files stay
on the local (ephemeral) filesystem since they are just intermediates.
"""

import json
import os
import time

DATA_DIR = "data"

_redis = None
_redis_tried = False


def _redis_client():
    """Return a connected Redis client if REDIS_URL is set, else None (cached)."""
    global _redis, _redis_tried
    if _redis_tried:
        return _redis
    _redis_tried = True
    url = os.environ.get("REDIS_URL")
    if not url:
        return None
    try:
        import redis
        client = redis.from_url(url, decode_responses=True)
        client.ping()
        _redis = client
        print("Cache store: using Redis.")
    except Exception as err:
        print(f"Cache store: Redis unavailable ({err}); using local files.")
        _redis = None
    return _redis


def cache_get_int(key):
    """Read a small integer from Redis (used to soft-cache live review counts)."""
    r = _redis_client()
    if r is None:
        return None
    try:
        v = r.get(key)
        return int(v) if v is not None else None
    except (ValueError, TypeError, Exception):
        return None


def cache_set_int(key, value, ttl):
    """Write a small integer to Redis with a TTL. No-op without Redis."""
    r = _redis_client()
    if r is None:
        return
    try:
        r.set(key, int(value), ex=int(ttl))
    except Exception:
        pass


def load_analysis(app_id, max_age_days):
    """Return the cached analysis dict if present and fresh, otherwise None."""
    r = _redis_client()
    if r is not None:
        try:
            raw = r.get(f"analysis:{app_id}")
            if raw:
                return json.loads(raw)
        except Exception as err:
            print(f"Redis read failed ({err}); checking local file.")
        # Redis miss/unavailable: fall through to the local file as a backup,
        # so a failed/denied Redis write does not force a full re-analysis.

    # Filesystem (primary with no Redis, secondary otherwise). Age-checked.
    path = os.path.join(DATA_DIR, f"analysis_{app_id}.json")
    if not os.path.exists(path):
        return None
    if (time.time() - os.path.getmtime(path)) >= max_age_days * 86400:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def save_analysis(app_id, analysis, max_age_days):
    """Persist the analysis: Redis (with a TTL) if configured, else a local file."""
    # Stamp the save time (shallow copy so we do not mutate the caller's dict).
    # refresh_status() reads this to enforce the per-visitor refresh cooldown.
    analysis = dict(analysis)
    analysis["cached_at"] = time.time()
    payload = json.dumps(analysis, ensure_ascii=False)
    r = _redis_client()
    if r is not None:
        try:
            r.set(f"analysis:{app_id}", payload, ex=int(max_age_days * 86400))
            return
        except Exception as err:
            print(f"Redis write failed ({err}); falling back to a local file.")
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, f"analysis_{app_id}.json"), "w", encoding="utf-8") as f:
        f.write(payload)
