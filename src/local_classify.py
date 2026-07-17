"""
local_classify.py

Fast, local classification distilled from the LLM. Loads the logistic-regression
models trained by train_classifiers.py (src/models/) and, for a batch of review
embeddings, predicts sentiment / category / constructive with a confidence gate.

A review is "confident" only if ALL THREE models clear their per-task threshold
(see below); the caller then keeps it local and sends the rest to the LLM. If the
models are absent (not trained/committed) everything degrades to LLM classification.
"""

import os

MODELS_DIR = os.path.join(os.path.dirname(__file__), "models")

# Per-task confidence gates. A review is kept local only when ALL THREE tasks
# clear their own threshold; otherwise it falls back to the LLM. Category is the
# binding constraint on how much we can offload, but it is low-stakes (it only
# colors the donut and a category tag), so it gates loosest to let more reviews
# skip the LLM. Sentiment (drives Issues/Praise routing) and constructive (the
# noise filter) gate tighter to keep their quality. LOCAL_CLASSIFY_THRESHOLD, if
# set, becomes the default for any task without its own LOCAL_THRESHOLD_* value.
_GLOBAL = os.environ.get("LOCAL_CLASSIFY_THRESHOLD")


def _thr(env_name, default):
    v = os.environ.get(env_name)
    if v is not None:
        return float(v)
    return float(_GLOBAL) if _GLOBAL is not None else default


T_SENTIMENT = _thr("LOCAL_THRESHOLD_SENTIMENT", 0.65)
T_CATEGORY = _thr("LOCAL_THRESHOLD_CATEGORY", 0.55)
T_CONSTRUCTIVE = _thr("LOCAL_THRESHOLD_CONSTRUCTIVE", 0.65)

# Speed-first mode: treat EVERY review as confident, so the hybrid classifier
# sends nothing to the LLM (classification becomes embed + local predict, a few
# seconds instead of minutes). Trades accuracy (esp. category ~69%) for speed,
# and pauses new LLM training-sample collection while it is on.
LOCAL_ONLY = os.environ.get("LOCAL_CLASSIFY_ONLY", "0").strip().lower() in ("1", "true", "yes", "on")

_models = None
_tried = False


def _load():
    global _models, _tried
    if _tried:
        return _models
    _tried = True
    try:
        import joblib
        _models = {
            "sentiment": joblib.load(os.path.join(MODELS_DIR, "clf_sentiment.joblib")),
            "category": joblib.load(os.path.join(MODELS_DIR, "clf_category.joblib")),
            "constructive": joblib.load(os.path.join(MODELS_DIR, "clf_constructive.joblib")),
        }
        mode = "LOCAL-ONLY (no LLM fallback)" if LOCAL_ONLY else (
            f"thresholds  sentiment {T_SENTIMENT}  category {T_CATEGORY}  constructive {T_CONSTRUCTIVE}")
        print(f"Local classifier loaded ({mode}).")
    except Exception as err:
        print(f"Local classifier unavailable ({err}); using LLM classification.")
        _models = None
    return _models


def available() -> bool:
    return _load() is not None


def classify(embeddings):
    """
    Return a list of {sentiment, category, is_constructive, confident, confidence}
    per embedding, or None if models are unavailable.
    """
    m = _load()
    if m is None:
        return None
    import numpy as np
    X = np.asarray(embeddings, dtype="float32")
    se_p, se_c = m["sentiment"].predict_proba(X), m["sentiment"].classes_
    ca_p, ca_c = m["category"].predict_proba(X), m["category"].classes_
    co_p, co_c = m["constructive"].predict_proba(X), m["constructive"].classes_
    out = []
    for i in range(len(X)):
        si, ci, oi = se_p[i].argmax(), ca_p[i].argmax(), co_p[i].argmax()
        confident = LOCAL_ONLY or (se_p[i][si] >= T_SENTIMENT and ca_p[i][ci] >= T_CATEGORY
                                   and co_p[i][oi] >= T_CONSTRUCTIVE)
        out.append({
            "sentiment": str(se_c[si]),
            "category": str(ca_c[ci]),
            "is_constructive": bool(int(co_c[oi])),
            "confident": bool(confident),
            # min of the three max-probs = how unsure the model is on its weakest
            # task for this review (drives active-learning training collection).
            "confidence": float(min(se_p[i][si], ca_p[i][ci], co_p[i][oi])),
        })
    return out
