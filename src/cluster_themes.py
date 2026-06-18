"""
cluster_themes.py

Embeddings-based theming.

Instead of asking the LLM to assign every single review to a theme (hundreds of
calls), we:
  1. embed each review's text once (one cheap batched OpenAI call),
  2. cluster the embeddings with HDBSCAN (it finds the number of themes on its
     own and drops outliers into a "noise" label), and
  3. make ONE small LLM call per cluster to name and describe it.

That turns ~hundreds of per-review LLM calls into a single embedding batch plus a
handful of label calls, so a first-time analysis is far faster and cheaper.

The output exactly mirrors themes.aggregate_themes, so the rest of the pipeline
and the whole report are unchanged. If anything here fails (no OpenAI key, no
scikit-learn, an API error), themes.analyze_both falls back to LLM theming.
"""

import os
from typing import Literal

from pydantic import BaseModel

# Reuse the category type, the theme schema/aggregator, and the batching limits.
from classify import ReviewCategory
from classify_batch import MAX_REVIEW_CHARS


# ----------------------------------------------------------------------------
# Tuning knobs
# ----------------------------------------------------------------------------

EMBED_MODEL = "text-embedding-3-small"   # cheap, 1536-dim OpenAI embeddings
EMBED_BATCH = 256                        # review texts per embeddings request
MIN_CLUSTER_SIZE = 4                     # smallest group HDBSCAN treats as a theme
LABEL_SAMPLE = 8                         # representative reviews shown to the labeler


class ClusterLabel(BaseModel):
    """Name/describe one cluster of reviews (one small LLM call per cluster)."""
    name: str                 # short, specific, e.g. "blatant cheaters in Premier"
    description: str          # one-line explanation
    kind: Literal["feature", "emotional"]


# ----------------------------------------------------------------------------
# Step 1: embed
# ----------------------------------------------------------------------------

def _openai_client():
    """A direct OpenAI client (embeddings live outside the llm.py wrapper)."""
    from openai import OpenAI
    key = os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_API_KEY")
    if not key or key.startswith("your_"):
        raise RuntimeError("OPENAI_API_KEY is required for embedding-based theming.")
    return OpenAI(api_key=key)


def embed_texts(texts: list) -> list:
    """Return one embedding vector per input text, in order, via OpenAI."""
    client = _openai_client()
    vectors = []
    for start in range(0, len(texts), EMBED_BATCH):
        # Embeddings reject empty strings, so substitute a single space.
        chunk = [((t or " ").strip()[:MAX_REVIEW_CHARS] or " ")
                 for t in texts[start:start + EMBED_BATCH]]
        resp = client.embeddings.create(model=EMBED_MODEL, input=chunk)
        vectors.extend(item.embedding for item in resp.data)
    return vectors


# ----------------------------------------------------------------------------
# Step 2: cluster
# ----------------------------------------------------------------------------

def cluster_vectors(vectors: list) -> list:
    """
    Cluster embedding vectors with HDBSCAN. Returns an integer label per vector;
    label -1 means "outlier" (no clear theme). Normalizing first makes euclidean
    distance behave like cosine distance, which is what embeddings want.
    """
    import numpy as np
    from sklearn.cluster import HDBSCAN

    X = np.asarray(vectors, dtype="float32")
    norms = np.linalg.norm(X, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    X = X / norms

    n = len(X)
    # Keep clusters sensible on small games without over-fragmenting big ones.
    min_size = max(2, min(MIN_CLUSTER_SIZE, (n // 8) or 2))
    model = HDBSCAN(min_cluster_size=min_size, metric="euclidean")
    return model.fit_predict(X).tolist()


# ----------------------------------------------------------------------------
# Step 3: label each cluster (one LLM call each)
# ----------------------------------------------------------------------------

def _majority_category(reviews: list) -> str:
    """The most common per-review category in a cluster (from classification)."""
    counts = {}
    for r in reviews:
        c = r.get("category", "other")
        counts[c] = counts.get(c, 0) + 1
    return max(counts, key=counts.get) if counts else "other"


def _label_cluster(client, reviews: list, category: str) -> ClusterLabel:
    """Ask the LLM to name/describe a single cluster from a few representatives."""
    from llm import generate_json

    sample = sorted(
        reviews,
        key=lambda r: (r.get("helpful_votes", 0), r.get("playtime_at_review_hours", 0)),
        reverse=True,
    )[:LABEL_SAMPLE]
    block = "\n".join(f"- {(r.get('text') or '').strip()[:MAX_REVIEW_CHARS]}" for r in sample)

    prompt = (
        "These player reviews of one video game all describe the SAME specific "
        f"theme (rough category: {category}).\n\n"
        "Give a short, SPECIFIC name (3-6 words), a one-line description, and a "
        "'kind': use \"feature\" if the theme is a specific, actionable aspect "
        "(a system, mechanic, bug, or piece of content) or \"emotional\" if it is "
        "general sentiment, mood, nostalgia, or a farewell.\n\n"
        f"Reviews:\n{block}"
    )
    return generate_json(client, prompt, ClusterLabel)


# ----------------------------------------------------------------------------
# Orchestration: one polarity group -> ranked theme records
# ----------------------------------------------------------------------------

def theme_group_embed(client, reviews: list) -> list:
    """
    Theme one polarity group (e.g. all negative-leaning constructive reviews) by
    embedding + clustering + per-cluster labeling. Returns the same record shape
    as themes.aggregate_themes, so callers do not need to change.
    """
    # Imported here to avoid a circular import at module load time.
    from themes import ThemeDef, aggregate_themes, UNCLEAR_LABEL

    if not reviews:
        return []

    vectors = embed_texts([r.get("text", "") for r in reviews])
    labels = cluster_vectors(vectors)

    # Bucket reviews by cluster label.
    clusters = {}
    for idx, lab in enumerate(labels):
        clusters.setdefault(int(lab), []).append(reviews[idx])

    theme_defs = []
    used_names = set()
    for lab, members in clusters.items():
        if lab == -1:
            # HDBSCAN outliers: constructive but no clear shared theme.
            for r in members:
                r["theme"] = UNCLEAR_LABEL
            continue

        category = _majority_category(members)
        try:
            labeled = _label_cluster(client, members, category)
            name = (labeled.name or "").strip() or f"theme {lab}"
            description, kind = labeled.description, labeled.kind
        except Exception as err:
            print(f"  Cluster label failed ({err}); using a generic name.")
            name, description, kind = f"theme {lab}", "", "feature"

        # aggregate_themes buckets by name, so names must be unique.
        base, n = name, 2
        while name.lower() in used_names:
            name = f"{base} ({n})"
            n += 1
        used_names.add(name.lower())

        for r in members:
            r["theme"] = name
        theme_defs.append(ThemeDef(name=name, description=description,
                                   category=category, kind=kind))

    # Reuse the existing aggregator: counts, impact scores, example quotes, and
    # the "unclear" bucket are all produced exactly as before.
    return aggregate_themes(reviews, theme_defs)
