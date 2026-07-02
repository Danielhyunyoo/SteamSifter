"""
train_classifiers.py  —  OFFLINE distillation tool (run locally with your key set)

    python src/train_classifiers.py training_samples.jsonl

Distills the LLM's classification into small, fast local classifiers. It reads
the (text, label) samples collected in production (download them from the admin
page: /admin/training.jsonl), embeds the text with text-embedding-3-small
(reusing the app's embed_texts, so it matches inference), trains a logistic
regression per task (sentiment / category / constructive), prints held-out
accuracy and a per-class report, and saves the models + label order to
src/models/ for the app to load at inference time.

This is a dev tool, not part of the running app. It needs OPENAI_API_KEY set and
scikit-learn + joblib installed (both already in requirements).
"""

import json
import os
import sys
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

MODELS_DIR = os.path.join(os.path.dirname(__file__), "models")
MIN_PER_CLASS = 25          # drop classes too rare to train/evaluate fairly
TEST_SIZE = 0.2


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
    clf = LogisticRegression(max_iter=1000, C=1.0, class_weight="balanced")
    clf.fit(Xtr, ytr)
    acc = accuracy_score(yte, clf.predict(Xte))
    print(f"\n=== {name} ===  held-out accuracy {acc:.3f}  "
          f"({len(yf)} samples, classes: {sorted(keep)})")
    print(classification_report(yte, clf.predict(Xte), zero_division=0))

    clf.fit(Xf, yf)   # refit on everything for the shipped model
    return clf, acc


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "training_samples.jsonl"
    rows = load_samples(path)
    print(f"Loaded {len(rows)} samples from {path}")
    if not rows:
        print("No samples found; nothing to train.")
        return

    print("Embedding texts (calls OpenAI; ~a few cents for tens of thousands)...")
    X = np.asarray(embed_texts([r["t"] for r in rows]), dtype="float32")

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
    print("Share these numbers and we'll wire in whichever tasks pass.")


if __name__ == "__main__":
    main()
