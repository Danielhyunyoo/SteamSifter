"""
cluster_themes.py

Scale-friendly theming via embeddings + k-means. Per-review LLM theme assignment
grows linearly with review count; this path embeds every review once, groups the
embeddings with k-means (adaptive cluster count, every review lands in a cluster),
and makes ONE LLM call per cluster to name it (run in parallel). LLM work stays
roughly constant regardless of review count. Classification is untouched and the
output mirrors themes.aggregate_themes, so the report is unchanged. analyze_both
falls back to LLM theming if anything here is unavailable or errors.
"""

import os
from typing import Literal
from concurrent.futures import ThreadPoolExecutor

from pydantic import BaseModel

from classify import ReviewCategory
from classify_batch import MAX_REVIEW_CHARS, MAX_WORKERS


EMBED_MODEL = "text-embedding-3-small"
EMBED_BATCH = 256
LABEL_SAMPLE = 12
MIN_THEME_SIZE = 3
MAX_THEMES_PER_SIDE = 12
REVIEWS_PER_THEME = 25
MERGE_SIMILARITY = float(os.environ.get("MERGE_SIMILARITY", "0.80"))
                                 # merge clusters whose centroids are at least
                                 # this cosine-similar (collapses duplicate themes)


class ClusterLabel(BaseModel):
    """Name/describe one cluster of reviews (one small LLM call per cluster)."""
    name: str
    description: str
    kind: Literal["feature", "emotional"]


class _MergeGroup(BaseModel):
    """A set of theme indices that describe the same thing, with a merged label."""
    members: list[int]
    name: str
    description: str
    kind: Literal["feature", "emotional"]


def _openai_client():
    """A direct OpenAI client (embeddings live outside the llm.py wrapper)."""
    from openai import OpenAI
    key = os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_API_KEY")
    if not key or key.startswith("your_"):
        raise RuntimeError("OPENAI_API_KEY is required for embedding-based theming.")
    return OpenAI(api_key=key)


def _embed_chunk(client, chunk):
    """Embed one chunk, with a small retry for transient rate-limit/network blips."""
    import time as _time
    for attempt in range(1, 4):
        try:
            resp = client.embeddings.create(model=EMBED_MODEL, input=chunk)
            return [item.embedding for item in resp.data]
        except Exception:
            if attempt == 3:
                raise
            _time.sleep(2 * attempt)


def embed_texts(texts):
    """
    Return one embedding vector per input text, IN ORDER, via OpenAI.

    Chunks are embedded CONCURRENTLY (each call only waits on the API), so a
    large input (thousands of reviews) no longer serializes into a long stall.
    pool.map preserves input order, so vectors stay aligned with texts.
    """
    client = _openai_client()
    chunks = [
        [((t or " ").strip()[:MAX_REVIEW_CHARS] or " ")
         for t in texts[start:start + EMBED_BATCH]]
        for start in range(0, len(texts), EMBED_BATCH)
    ]
    if not chunks:
        return []
    if len(chunks) == 1:
        return _embed_chunk(client, chunks[0])
    vectors = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for chunk_vecs in pool.map(lambda c: _embed_chunk(client, c), chunks):
            vectors.extend(chunk_vecs)
    return vectors


def _adaptive_k(n):
    """Pick a sensible cluster count from review volume."""
    if n < 6:
        return 1
    return max(3, min(MAX_THEMES_PER_SIDE, n // REVIEWS_PER_THEME))


def cluster_vectors(vectors):
    """Group embedding vectors with k-means; return a cluster label per vector."""
    import numpy as np
    from sklearn.cluster import KMeans
    from sklearn.decomposition import PCA

    X = np.asarray(vectors, dtype="float32")
    n = len(X)
    if n < 3:
        return [0] * n

    norms = np.linalg.norm(X, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    X = X / norms

    if X.shape[1] > 50 and n > 10:
        X = PCA(n_components=min(50, n - 1), random_state=0).fit_transform(X)

    k = min(_adaptive_k(n), n)
    if k <= 1:
        return [0] * n
    return KMeans(n_clusters=k, n_init=10, random_state=0).fit_predict(X).tolist()


def _majority_category(reviews):
    """The most common per-review category in a cluster (from classification)."""
    counts = {}
    for r in reviews:
        c = r.get("category", "other")
        counts[c] = counts.get(c, 0) + 1
    return max(counts, key=counts.get) if counts else "other"


def _label_cluster(client, reviews, category):
    """Ask the LLM to name/describe a single cluster from a few representatives."""
    from llm import generate_json

    sample = sorted(
        reviews,
        key=lambda r: (r.get("helpful_votes", 0), r.get("playtime_at_review_hours", 0)),
        reverse=True,
    )[:LABEL_SAMPLE]
    block = "\n".join(f"- {(r.get('text') or '').strip()[:MAX_REVIEW_CHARS]}" for r in sample)

    prompt = (
        "Below are player reviews of one video game on Steam, grouped together "
        f"because they share the same topic (rough category: {category}).\n\n"
        "Name the single CONCRETE thing these reviews are actually about. Be "
        "specific and honest about the real issue or the real thing players "
        "praise: if they are about cheaters, say cheating; if about lag or FPS, "
        "say performance; if about prices or skins, say monetization.\n\n"
        "Name rules (3-6 words):\n"
        "- Be concrete and specific. Good: 'Rampant cheaters in competitive', "
        "'Frequent FPS drops after update', 'Toxic voice chat and griefing', "
        "'Loved gunplay and movement feel'.\n"
        "- Never use vague umbrella phrases. Bad: 'Mixed player experience', "
        "'Overall sentiment', 'General gameplay feedback', 'Player interaction'.\n"
        "- If one specific complaint or praise dominates the reviews, name THAT, "
        "even if a few reviews differ.\n\n"
        "Also give a one-line description and a 'kind': use feature for a "
        "specific, actionable aspect (a system, mechanic, bug, or content), or "
        "emotional for general mood, nostalgia, or a farewell.\n\n"
        f"Reviews:\n{block}"
    )
    return generate_json(client, prompt, ClusterLabel)


def merge_similar_clusters(vectors, labels):
    """
    Collapse clusters whose centroids are nearly identical (cosine >= the
    threshold), so a homogeneous game does not get the same idea split into many
    near-duplicate themes. Distinct topics keep low centroid similarity and stay
    separate. Returns a new labels list with merged clusters sharing one id.
    """
    import numpy as np

    X = np.asarray(vectors, dtype="float32")
    norms = np.linalg.norm(X, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    X = X / norms

    uniq = sorted(set(labels))
    if len(uniq) < 2:
        return list(labels)

    # Normalized centroid per cluster.
    centroid = {}
    for lab in uniq:
        idx = [i for i, l in enumerate(labels) if l == lab]
        c = X[idx].mean(axis=0)
        nc = np.linalg.norm(c)
        centroid[lab] = c / nc if nc else c

    # Union-find: merge any pair of clusters above the similarity threshold.
    parent = {lab: lab for lab in uniq}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i in range(len(uniq)):
        for j in range(i + 1, len(uniq)):
            a, b = uniq[i], uniq[j]
            if float(np.dot(centroid[a], centroid[b])) >= MERGE_SIMILARITY:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)   # keep the lower id as rep

    return [find(l) for l in labels]


def _dedupe_themes(client, labeled):
    """
    Merge themes that describe the SAME underlying issue worded differently.

    One LLM call reads every theme's name + description and returns groups of
    duplicates to fold together. This catches semantic duplicates that the
    embedding-centroid merge misses (e.g. "Lack of anti-cheat" vs "Rampant
    cheating and ineffective anti-cheat"). Robust to omissions: any theme the
    model does not place in a group is kept on its own.

    labeled / return: list of (members, name, description, kind, category).
    """
    from llm import generate_json

    if len(labeled) < 2:
        return labeled

    block = "\n".join(f"{i}. {name} \u2014 {desc}"
                      for i, (members, name, desc, kind, cat) in enumerate(labeled))
    prompt = (
        "Below is a numbered list of themes extracted from one video game's "
        "reviews. Some describe the SAME underlying issue or praise using "
        "different wording; others are genuinely distinct.\n\n"
        "Group together only the ones that are true duplicates of each other. "
        "Return a list of groups covering EVERY theme exactly once: each group "
        "gives the member indices and one best name, a one-line description, and "
        "a kind ('feature' or 'emotional') for the merged theme. A theme with no "
        "duplicate is its own group of one. Do NOT merge themes that are merely "
        "related but about different things.\n\n"
        f"Themes:\n{block}"
    )
    try:
        groups = generate_json(client, prompt, list[_MergeGroup]) or []
    except Exception as err:
        print(f"  Theme dedupe failed ({err}); keeping themes as-is.")
        return labeled

    n = len(labeled)
    assigned = set()
    merged = []
    for g in groups:
        idxs = [i for i in g.members if 0 <= i < n and i not in assigned]
        if not idxs:
            continue
        assigned.update(idxs)
        members = []
        for i in idxs:
            members.extend(labeled[i][0])
        name = (g.name or "").strip() or labeled[idxs[0]][1]
        merged.append((members, name, g.description, g.kind, _majority_category(members)))

    # Any theme the model didn't mention stays on its own.
    for i in range(n):
        if i not in assigned:
            merged.append(labeled[i])
    return merged


def theme_group_embed(client, reviews):
    """Theme one polarity group by embedding + k-means + per-cluster labeling."""
    from themes import ThemeDef, aggregate_themes, UNCLEAR_LABEL

    if not reviews:
        return []

    if reviews and all("_embedding" in r for r in reviews):
        vectors = [r["_embedding"] for r in reviews]   # reuse the classifier embeddings
    else:
        vectors = embed_texts([r.get("text", "") for r in reviews])
    labels = cluster_vectors(vectors)
    labels = merge_similar_clusters(vectors, labels)   # collapse duplicate themes

    clusters = {}
    for idx, lab in enumerate(labels):
        clusters.setdefault(int(lab), []).append(reviews[idx])

    real = {lab: m for lab, m in clusters.items() if len(m) >= MIN_THEME_SIZE}
    for lab, members in clusters.items():
        if lab not in real:
            for r in members:
                r["theme"] = UNCLEAR_LABEL

    def label(item):
        lab, members = item
        category = _majority_category(members)
        try:
            res = _label_cluster(client, members, category)
            name = (res.name or "").strip() or f"theme {lab}"
            return members, name, res.description, res.kind, category
        except Exception as err:
            print(f"  Cluster label failed ({err}); using a generic name.")
            return members, f"theme {lab}", "", "feature", category

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        labeled = list(pool.map(label, real.items()))

    # One LLM pass to merge themes that are the same idea worded differently.
    labeled = _dedupe_themes(client, labeled)

    theme_defs = []
    used_names = set()
    for members, name, description, kind, category in labeled:
        base, n = name, 2
        while name.lower() in used_names:
            name = f"{base} ({n})"
            n += 1
        used_names.add(name.lower())
        for r in members:
            r["theme"] = name
        theme_defs.append(ThemeDef(name=name, description=description,
                                   category=category, kind=kind))

    return aggregate_themes(reviews, theme_defs)
