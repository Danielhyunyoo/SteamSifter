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
import threading
import time
import uuid

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


# ----------------------------------------------------------------------------
# Background-job store: shared across gunicorn workers via Redis, with an
# in-memory fallback for single-process dev. Holds progress for the live bar and
# an appid -> job de-duplication claim so two people analyzing the same game
# share one run.
# ----------------------------------------------------------------------------

JOB_TTL = 900   # seconds a job record lives (Redis TTL auto-cleans finished jobs)

# Jobs use Redis only with multiple workers; a single worker keeps them in-process
# to avoid unnecessary Redis traffic on the free tier.
JOBS_USE_REDIS = int(os.environ.get("WEB_CONCURRENCY", "1")) > 1


def _job_redis():
    """The Redis client for jobs, or None when single-worker (use in-memory)."""
    return _redis_client() if JOBS_USE_REDIS else None

_mem_jobs = {}
_mem_active = {}
_mem_lock = threading.Lock()


def _mem_prune():
    """Drop finished in-memory jobs older than JOB_TTL (call under _mem_lock)."""
    now = time.time()
    stale = [jid for jid, j in _mem_jobs.items()
             if j.get("done") and (now - j.get("finished_at", now)) > JOB_TTL]
    for jid in stale:
        _mem_jobs.pop(jid, None)


def job_begin(appid):
    """
    Begin or attach to an analysis job for a game. Returns (job_id, is_new).

    De-duplicates: if a job for this appid is already in flight, returns its id
    with is_new=False so the caller does NOT start a second run. Atomic across
    workers via Redis SET NX; falls back to a per-process lock without Redis.
    """
    new_id = uuid.uuid4().hex
    r = _job_redis()
    if r is not None:
        try:
            claimed = r.set(f"active:{appid}", new_id, nx=True, ex=JOB_TTL)
            if not claimed:
                existing = r.get(f"active:{appid}")
                if existing:
                    return existing, False
                r.set(f"active:{appid}", new_id, ex=JOB_TTL)   # claim vanished; take it
            r.hset(f"job:{new_id}", mapping={"percent": 0, "message": "Starting...",
                                             "done": 0, "error": "", "appid": appid, "started_at": time.time()})
            r.expire(f"job:{new_id}", JOB_TTL)
            return new_id, True
        except Exception as err:
            print(f"Redis job_begin failed ({err}); using in-memory jobs.")
    with _mem_lock:
        _mem_prune()
        existing = _mem_active.get(appid)
        if existing and not _mem_jobs.get(existing, {}).get("done"):
            return existing, False
        _mem_jobs[new_id] = {"percent": 0, "message": "Starting...", "done": False,
                             "error": None, "appid": appid, "started_at": time.time()}
        _mem_active[appid] = new_id
        return new_id, True


def job_update(job_id, percent=None, message=None):
    """Record a running job's progress (best-effort)."""
    r = _job_redis()
    if r is not None:
        try:
            fields = {}
            if percent is not None:
                fields["percent"] = int(percent)
            if message is not None:
                fields["message"] = message
            if fields:
                r.hset(f"job:{job_id}", mapping=fields)
                r.expire(f"job:{job_id}", JOB_TTL)
            return
        except Exception:
            pass
    with _mem_lock:
        j = _mem_jobs.get(job_id)
        if j:
            if percent is not None:
                j["percent"] = percent
            if message is not None:
                j["message"] = message


def job_finish(appid, job_id, error=None):
    """Mark a job done and release the appid so a fresh run can start later."""
    r = _job_redis()
    if r is not None:
        try:
            fields = {"done": 1, "error": error or ""}
            if not error:
                fields["percent"] = 100
                fields["message"] = "Done"
            r.hset(f"job:{job_id}", mapping=fields)
            r.expire(f"job:{job_id}", JOB_TTL)
            if r.get(f"active:{appid}") == job_id:
                r.delete(f"active:{appid}")
            return
        except Exception:
            pass
    with _mem_lock:
        j = _mem_jobs.get(job_id)
        if j:
            j.update(done=True, error=error, finished_at=time.time())
            if not error:
                j.update(percent=100, message="Done")
        if _mem_active.get(appid) == job_id:
            _mem_active.pop(appid, None)


def job_get(job_id):
    """Return a job's state dict (percent/message/done/error), or None if unknown."""
    r = _job_redis()
    if r is not None:
        try:
            h = r.hgetall(f"job:{job_id}")
            if h:
                sa = float(h.get("started_at") or 0)
                return {"percent": int(h.get("percent", 0)),
                        "message": h.get("message", ""),
                        "done": h.get("done") == "1",
                        "error": h.get("error") or None,
                        "started_at": sa,
                        "elapsed": max(0.0, time.time() - sa) if sa else 0.0}
            return None
        except Exception:
            pass
    with _mem_lock:
        j = _mem_jobs.get(job_id)
        if not j:
            return None
        out = dict(j)
        sa = out.get('started_at') or 0
        out['elapsed'] = max(0.0, time.time() - sa) if sa else 0.0
        return out


# ----------------------------------------------------------------------------
# Site config: owner-managed home-page announcements and a seasonal gradient
# theme. Stored as one small JSON blob (Redis when configured, else a local file
# for dev). Expired announcements and an expired theme are pruned lazily on read.
# ----------------------------------------------------------------------------

SITE_CONFIG_KEY = "site:config"
SITE_CONFIG_FILE = os.path.join(DATA_DIR, "site_config.json")


def _site_load():
    """Load the whole site-config blob: {"announcements": [...], "theme": {...}|None}."""
    r = _redis_client()
    if r is not None:
        try:
            raw = r.get(SITE_CONFIG_KEY)
            return json.loads(raw) if raw else {"announcements": [], "theme": None}
        except Exception as err:
            print(f"Site-config Redis read failed ({err}); using local file.")
    try:
        with open(SITE_CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"announcements": [], "theme": None}


def _site_save(cfg):
    """Persist the whole site-config blob (Redis with no TTL, else a local file)."""
    payload = json.dumps(cfg, ensure_ascii=False)
    r = _redis_client()
    if r is not None:
        try:
            r.set(SITE_CONFIG_KEY, payload)   # persists until changed (no TTL)
            return
        except Exception as err:
            print(f"Site-config Redis write failed ({err}); using local file.")
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(SITE_CONFIG_FILE, "w", encoding="utf-8") as f:
        f.write(payload)


def _site_prune(cfg):
    """Drop expired announcements and an expired theme. Returns True if changed."""
    now = time.time()
    anns = cfg.get("announcements") or []
    kept = [a for a in anns if not a.get("expires_at") or a["expires_at"] > now]
    changed = len(kept) != len(anns)
    cfg["announcements"] = kept
    theme = cfg.get("theme")
    if theme and theme.get("expires_at") and theme["expires_at"] <= now:
        cfg["theme"] = None
        changed = True
    return changed


def announcements_active():
    """Active (non-expired) announcements, newest first."""
    cfg = _site_load()
    if _site_prune(cfg):
        _site_save(cfg)
    return sorted(cfg.get("announcements") or [],
                  key=lambda a: a.get("created_at", 0), reverse=True)


def announcement_add(title, message, ttl_seconds):
    """Add an announcement that expires after ttl_seconds (0/None = no expiry)."""
    cfg = _site_load()
    _site_prune(cfg)
    now = time.time()
    cfg.setdefault("announcements", []).append({
        "id": uuid.uuid4().hex[:8], "title": title, "message": message,
        "created_at": now,
        "expires_at": (now + ttl_seconds) if ttl_seconds else None,
    })
    _site_save(cfg)


def announcement_delete(ann_id):
    """Remove one announcement by id."""
    cfg = _site_load()
    cfg["announcements"] = [a for a in (cfg.get("announcements") or [])
                            if a.get("id") != ann_id]
    _site_prune(cfg)
    _site_save(cfg)


def theme_active():
    """The active seasonal gradient theme dict, or None if unset/expired."""
    cfg = _site_load()
    if _site_prune(cfg):
        _site_save(cfg)
    return cfg.get("theme")


def theme_set(grad_top, grad_bottom, ttl_seconds=None):
    """Set the seasonal gradient. ttl_seconds falsy = stays until removed."""
    cfg = _site_load()
    now = time.time()
    cfg["theme"] = {"grad_top": grad_top, "grad_bottom": grad_bottom,
                    "set_at": now,
                    "expires_at": (now + ttl_seconds) if ttl_seconds else None}
    _site_save(cfg)


def theme_clear():
    """Turn the seasonal theme off."""
    cfg = _site_load()
    cfg["theme"] = None
    _site_save(cfg)


def training_baseline_get():
    """The {count, at} snapshot recorded at the last training-data download."""
    return _site_load().get("training_baseline")


def training_baseline_set(count):
    """Record the current sample count as the last-downloaded baseline, so the
    admin page can show how many samples are new since the last retrain."""
    cfg = _site_load()
    cfg["training_baseline"] = {"count": int(count), "at": time.time()}
    _site_save(cfg)
