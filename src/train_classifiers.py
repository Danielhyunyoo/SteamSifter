"""
train_classifiers.py  —  OFFLINE distillation tool (run locally with your key set)

    python src/train_classifiers.py training_samples.jsonl

Distills the LLM's classification into small, fast local classifiers. Reads the
(text, label) samples collected in production (download from /admin/training.jsonl),
embeds the text with text-embedding-3-small (reusing the app's embed_texts, so it
matches inference), trains a logistic regression per task (sentiment / category /
constructive), prints held-out accuracy and a per-class report, and saves the
models to src/models/ for the app to load at inference time.

Embeddings are cached to disk (keyed by text hash), so a rerun only embeds NEW
samples and a mid-run network failure never throws away work already done. That
makes each retrain after a data bump cheap: only the newly-collected samples are
sent to OpenAI.

Dev tool, not part of the running app. Needs OPENAI_API_KEY set and scikit-learn
+ joblib installed (both already in requirements).

Env overrides (optional):
    MODELS_DIR    where to write clf_*.joblib   (default: src/models)
    EMBED_CACHE   embedding cache file          (default: <repo>/.embed_cache.joblib)
"""

import hashlib
import json
import os
import sys
import time
from collections import Counter

import numpy as np
import joblib
try:
    from dotenv import load_dotenv
    load_dotenv()   # pick up OPENAI_API_KEY from a local .env if present
except Exception:
    pass
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score, classification_report

from cluster_themes import embed_texts

_HERE = os.path.dirname(__file__)
MODELS_DIR = os.environ.get("MODELS_DIR", os.path.join(_HERE, "models"))
EMBED_CACHE = os.environ.get(
    "EMBED_CACHE", os.path.join(_HERE, os.pardir, ".embed_cache.joblib"))
# "balanced" up-weights rare classes (better recall, worse precision); for a
# confidence-gated distiller, class_weight=None usually makes confident
# predictions more trustworthy (uncertain ones fall back to the LLM anyway).
# Default None won the A/B on the 76k set (more coverage AND higher accuracy
# when confident). Set CLASS_WEIGHT=balanced to restore the old behavior.
CLASS_WEIGHT = "balanced" if os.environ.get("CLASS_WEIGHT", "none").lower() \
    == "balanced" else None
MIN_PER_CLASS = 25          # drop classes too rare to train/evaluate fairly
TEST_SIZE = 0.2
EMBED_BATCH = 256           # texts per OpenAI embedding call
CHECKPOINT_EVERY = 10000    # persist the cache after this many new embeddings


def load_samples(path):
    """Read the JSONL training dump; keep rows that have real text."""
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if (r.get("t") or "").strip():
                rows.append(r)
    return rows


def _hash(text):
    """Stable key for the embedding cache."""
    return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()


def _load_cache(path):
    if os.path.exists(path):
        try:
            c = joblib.load(path)
            print(f"Loaded {len(c)} cached embeddings from {path}")
            return c
        except Exception as e:
            print(f"Could not read embed cache ({e}); starting fresh.")
    return {}


def embed_with_cache(texts):
    """
    Embed every text, reusing a disk cache keyed by text hash. Only texts not
    already cached are sent to OpenAI, in batches, with a retry on transient
    failures and periodic checkpoints so a long run is resumable.
    """
    cache = _load_cache(EMBED_CACHE)

    # Distinct texts that still need embedding.
    need, seen = [], set()
    for t in texts:
        h = _hash(t)
        if h not in cache and h not in seen:
            seen.add(h)
            need.append(t)

    if not need:
        print(f"All {len(texts)} embeddings served from cache.")
    else:
        print(f"Embedding {len(need)} new texts "
              f"({len(texts) - len(need)} already cached). This calls OpenAI...")
        since_ckpt = 0
        for start in range(0, len(need), EMBED_BATCH):
            chunk = need[start:start + EMBED_BATCH]
            for attempt in range(1, 6):
                try:
                    vecs = embed_texts(chunk)
                    break
                except Exception as e:
                    if attempt == 5:
                        raise
                    wait = 2 * attempt
                    print(f"  embed batch failed ({e}); retry {attempt}/4 in {wait}s")
                    time.sleep(wait)
            for t, v in zip(chunk, vecs):
                cache[_hash(t)] = np.asarray(v, dtype="float32")
            since_ckpt += len(chunk)
            done = min(start + EMBED_BATCH, len(need))
            print(f"  embedded {done}/{len(need)}")
            if since_ckpt >= CHECKPOINT_EVERY:
                joblib.dump(cache, EMBED_CACHE)
                since_ckpt = 0
                print(f"  ...checkpointed cache ({len(cache)} vectors)")
        joblib.dump(cache, EMBED_CACHE)
        print(f"Cache saved to {EMBED_CACHE} ({len(cache)} vectors).")

    return np.asarray([cache[_hash(t)] for t in texts], dtype="float32")


def confidence_report(name, clf, Xte, yte):
    """
    Show what the model does WHEN CONFIDENT, the metric that actually matters for
    the app's confidence-gated hybrid: at each probability threshold, how many
    held-out reviews the model is sure about (coverage = offloaded from the LLM)
    and how accurate it is on exactly those.
    """
    proba = clf.predict_proba(Xte)
    pred = clf.classes_[proba.argmax(axis=1)]
    conf = proba.max(axis=1)
    yte = np.asarray(yte)
    print(f"  confidence gating ({name}):  threshold -> coverage / accuracy-when-confident")
    for thr in (0.5, 0.6, 0.7, 0.8, 0.9):
        mask = conf >= thr
        cov = float(mask.mean())
        acc = float((pred[mask] == yte[mask]).mean()) if mask.any() else 0.0
        print(f"    >= {thr:.1f}   covers {cov*100:5.1f}%   acc {acc*100:5.1f}%")


def train_task(name, X, labels):
    """Train + evaluate one classifier; returns (fitted_clf, accuracy)."""
    counts = Counter(labels)
    keep = {k for k, v in counts.items() if v >= MIN_PER_CLASS}
    idx = [i for i, y in enumerate(labels) if y in keep]
    if len({labels[i] for i in idx}) < 2:
        print(f"\n=== {name} ===  SKIPPED (not enough labeled classes)")
        return None, 0.0
    Xf = X[idx]
    yf = [labels[i] for i in idx]

    Xtr, Xte, ytr, yte = train_test_split(
        Xf, yf, test_size=TEST_SIZE, random_state=0, stratify=yf)
    clf = LogisticRegression(max_iter=2000, C=1.0, class_weight=CLASS_WEIGHT)
    clf.fit(Xtr, ytr)
    acc = accuracy_score(yte, clf.predict(Xte))
    print(f"\n=== {name} ===  held-out accuracy {acc:.3f}  "
          f"({len(yf)} samples, classes: {sorted(keep)})")
    print(classification_report(yte, clf.predict(Xte), zero_division=0))
    confidence_report(name, clf, Xte, yte)

    clf.fit(Xf, yf)   # refit on everything for the shipped model
    return clf, acc


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "training_samples.jsonl"
    rows = load_samples(path)
    print(f"Loaded {len(rows)} samples from {path}")
    if not rows:
        print("No samples found; nothing to train.")
        return

    # Label balance up front, so starved classes are obvious before training.
    for key, default in (("se", "neutral"), ("ca", "other"), ("co", 1)):
        c = Counter(str(r.get(key, default)) for r in rows)
        print(f"  {key:>3} distribution: {dict(c.most_common())}")

    X = embed_with_cache([r["t"] for r in rows])

    os.makedirs(MODELS_DIR, exist_ok=True)
    tasks = {
        "sentiment": [r.get("se", "neutral") for r in rows],
        "category": [r.get("ca", "other") for r in rows],
        "constructive": [int(r.get("co", 1)) for r in rows],
    }
    summary = {}
    for name, labels in tasks.items():
        clf, acc = train_task(name, X, labels)
        if clf is not None:
            joblib.dump(clf, os.path.join(MODELS_DIR, f"clf_{name}.joblib"))
            summary[name] = round(acc, 3)

    print("\n---------------------------------------------")
    print("Accuracy summary:", summary)
    print(f"Models saved to {MODELS_DIR}")
    print("Share these numbers and we'll compare against the last run.")


if __name__ == "__main__":
    main()
