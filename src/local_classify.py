"""
local_classify.py

Fast, local classification distilled from the LLM. Loads the logistic-regression
models trained by train_classifiers.py (src/models/) and, for a batch of review
embeddings, predicts sentiment / category / constructive with a confidence gate.

A review is "confident" only if ALL THREE models clear LOCAL_CLASSIFY_THRESHOLD;
the caller then keeps it local and sends the rest to the LLM. If the models are
absent (not trained/committed) everything degrades to LLM classification.
"""

import os

MODELS_DIR = os.path.join(os.path.dirname(__file__), "models")
THRESHOLD = float(os.environ.get("LOCAL_CLASSIFY_THRESHOLD", "0.7"))

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
        print(f"Local classifier loaded (confidence threshold {THRESHOLD}).")
    except Exception as err:
        print(f"Local classifier unavailable ({err}); using LLM classification.")
        _models = None
    return _models


def available() -> bool:
    return _load() is not None


def classify(embeddings):
    """
    Return a list of {sentiment, category, is_constructive, confident} per
    embedding, or None if models are unavailable.
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
        confident = (se_p[i][si] >= THRESHOLD and ca_p[i][ci] >= THRESHOLD
                     and co_p[i][oi] >= THRESHOLD)
        out.append({
            "sentiment": str(se_c[si]),
            "category": str(ca_c[ci]),
            "is_constructive": bool(int(co_c[oi])),
            "confident": bool(confident),
        })
    return out
