"""
training_data.py

Passively collect (review text, LLM label) samples during each fresh analysis,
so a small local classifier can later be distilled from the LLM to cut the
per-review classification load.

We persist the TEXT and labels, not the 1536-dim embedding: embeddings are cheap
to re-derive at training time, and storing text keeps the dataset ~40x smaller,
so it fits in the existing Redis with no extra service. Stored as a capped Redis
list when available, else a local JSONL file. Strictly best-effort: any failure
is swallowed so it never affects an analysis.
"""

import json
import os

SAMPLES_KEY = "training:samples"
MAX_SAMPLES = int(os.environ.get("TRAINING_MAX_SAMPLES", "100000"))
LOCAL_PATH = os.path.join("data", "training_samples.jsonl")
TEXT_CAP = 600
_CHUNK = 500


def _redis():
    try:
        from store import _redis_client
        return _redis_client()
    except Exception:
        return None


def _record(r):
    """The compact training row for one classified review."""
    return {
        "t": (r.get("text") or "").strip()[:TEXT_CAP],
        "se": r.get("sentiment", "neutral"),       # LLM sentiment label
        "ca": r.get("category", "other"),          # LLM category label
        "co": 1 if r.get("is_constructive", True) else 0,   # constructive vs noise
        "lang": r.get("language") or "",
    }


def log_samples(classified):
    """Append one (text, label) record per classified review. Best-effort."""
    recs = [json.dumps(_record(r), ensure_ascii=False)
            for r in classified if (r.get("text") or "").strip()]
    if not recs:
        return
    rc = _redis()
    if rc is not None:
        try:
            for i in range(0, len(recs), _CHUNK):
                rc.rpush(SAMPLES_KEY, *recs[i:i + _CHUNK])
            rc.ltrim(SAMPLES_KEY, -MAX_SAMPLES, -1)   # keep only the newest N
            return
        except Exception as err:
            print(f"training_data: Redis log failed ({err}); using local file.")
    try:
        os.makedirs(os.path.dirname(LOCAL_PATH), exist_ok=True)
        with open(LOCAL_PATH, "a", encoding="utf-8") as f:
            f.write("\n".join(recs) + "\n")
    except Exception as err:
        print(f"training_data: local log failed ({err}).")


def count():
    """How many samples have been collected so far."""
    rc = _redis()
    if rc is not None:
        try:
            return rc.llen(SAMPLES_KEY)
        except Exception:
            pass
    if os.path.exists(LOCAL_PATH):
        with open(LOCAL_PATH, encoding="utf-8") as f:
            return sum(1 for _ in f)
    return 0


def export_jsonl():
    """
    Yield the whole dataset as JSONL lines (for the admin download).

    Pages through the Redis list in chunks. A single LRANGE 0 -1 over tens of
    thousands of items can exceed a managed-Redis (e.g. Upstash) response-size
    limit and raise, which previously got swallowed and produced a silent 0-byte
    download even though the samples were safely stored.
    """
    rc = _redis()
    if rc is not None:
        try:
            total = rc.llen(SAMPLES_KEY)
        except Exception as err:
            print(f"training_data: Redis llen failed ({err}); trying local file.")
            total = 0
        if total:
            for start in range(0, total, _CHUNK):
                # LRANGE is inclusive on both ends.
                for raw in rc.lrange(SAMPLES_KEY, start, start + _CHUNK - 1):
                    yield raw + "\n"
            return
    if os.path.exists(LOCAL_PATH):
        with open(LOCAL_PATH, encoding="utf-8") as f:
            for line in f:
                yield line
