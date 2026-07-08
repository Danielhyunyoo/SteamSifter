"""
classify_batch.py

Classifies a whole file of reviews efficiently, in batches.

Why batch? Free LLM tiers limit how many requests you can make per minute.
Sending one API call per review (300 calls) would be slow and would trip the
rate limit. Instead we send ~20 reviews per call, so 300 reviews becomes ~15
calls. We also pause briefly between calls to stay comfortably under the limit,
and retry once if we still get throttled.

Run directly to classify a saved reviews file and write an enriched copy:
    python src/classify_batch.py data/reviews_730_negative.json
"""

import argparse
import json
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Literal

from pydantic import BaseModel

# Reuse the client/helper and the category list we already defined.
from llm import get_client, generate_json
from classify import ReviewCategory


# ----------------------------------------------------------------------------
# Tuning knobs
# ----------------------------------------------------------------------------

BATCH_SIZE = int(os.environ.get("LLM_BATCH_SIZE", "40"))
                                # reviews per API call; smaller batches return
                                # faster and overlap better when run concurrently.
                                # Raise (e.g. 60-80) to cut the number of calls at
                                # scale; watch structured-output reliability + tokens.
MAX_WORKERS = int(os.environ.get("LLM_MAX_WORKERS", "8"))
                                # how many batch calls run at once. Paid OpenAI
                                # handles this easily; set LLM_MAX_WORKERS=1-2 for a
                                # free Gemini key to respect its low requests/min.
DELAY_BETWEEN_BATCHES = 2.0     # backoff base, used only when a call fails (e.g. 429)
MAX_REVIEW_CHARS = 500          # truncate very long reviews to control token use
MAX_RETRIES = 3                 # retries when a batch call fails (e.g. throttled)


# ----------------------------------------------------------------------------
# Batch schema
# ----------------------------------------------------------------------------

class BatchClassification(BaseModel):
    """
    One classified review within a batch.

    'index' refers to the review's position WITHIN the batch prompt, so we can
    map each result back to the correct review even if the model reorders them.
    """
    index: int
    sentiment: Literal["positive", "negative", "neutral"]
    category: ReviewCategory
    is_constructive: bool   # True = real on-topic feedback; False = noise/junk


# ----------------------------------------------------------------------------
# Building the batch prompt
# ----------------------------------------------------------------------------

def build_batch_prompt(texts: list, context: str = "") -> str:
    """
    Build a single prompt that lists several reviews, each with its batch index.

    Args:
        texts: The review texts for this batch.

    Returns:
        A prompt string asking the model to classify every listed review.
    """
    # Number each review so the model can reference it by index in its answer.
    numbered = []
    for i, text in enumerate(texts):
        snippet = (text or "").strip()[:MAX_REVIEW_CHARS]
        numbered.append(f"Review {i}:\n{snippet}")

    reviews_block = "\n\n".join(numbered)
    context_block = f"About this game (for context):\n{context}\n\n" if context else ""

    return (
        context_block +
        "You are classifying players' reviews of a video game on Steam.\n"
        "For EACH review below, return its index, sentiment "
        "(positive/negative/neutral), the single best category from:\n"
        "  bug, performance, gameplay, cheating, community, monetization, "
        "content, ui_ux, praise, other,\n"
        "and is_constructive: true if the review gives real, on-topic feedback "
        "about the game; false if it is noise (a joke, meme, one-word or empty "
        "review, off-topic rant, review-bomb spam, or sarcasm not meant "
        "literally). Use the game context above to catch sarcasm: praise that "
        "contradicts what the game actually is (e.g. calling a violent or "
        "adult-only game 'family friendly' or 'great for kids') is a joke, not "
        "real feedback, so mark it is_constructive=false.\n\n"
        "Return one classification object per review.\n\n"
        f"{reviews_block}"
    )


# ----------------------------------------------------------------------------
# Classifying one batch (with retry on failure)
# ----------------------------------------------------------------------------

def classify_batch(client, texts: list, context: str = "") -> list:
    """
    Classify one batch of review texts, retrying if the call fails.

    Args:
        client: An LLM client from get_client().
        texts:  The review texts for this batch.

    Returns:
        A list of BatchClassification objects (possibly fewer than len(texts)
        if the model omits some; the caller fills any gaps).
    """
    prompt = build_batch_prompt(texts, context)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            # a list[...] schema makes the model return a JSON array.
            results = generate_json(client, prompt, list[BatchClassification])
            return results or []
        except Exception as err:
            # Most likely a rate-limit (429) hiccup. Back off and try again.
            wait = DELAY_BETWEEN_BATCHES * attempt
            print(f"  Batch failed (attempt {attempt}/{MAX_RETRIES}): {err}")
            if attempt < MAX_RETRIES:
                print(f"  Waiting {wait:.0f}s before retrying...")
                time.sleep(wait)

    # If every retry failed, return empty so the caller can fall back gracefully.
    print("  Giving up on this batch after retries.")
    return []


# ----------------------------------------------------------------------------
# Classifying a whole list of reviews
# ----------------------------------------------------------------------------

def _classify_one(client, batch: list, context: str = "") -> None:
    """Classify one batch and write the labels onto its review dicts in place."""
    results = classify_batch(client, [r["text"] for r in batch], context)
    by_index = {r.index: r for r in results}
    for i, review in enumerate(batch):
        label = by_index.get(i)
        if label is not None:
            review["sentiment"] = label.sentiment
            review["category"] = label.category
            review["is_constructive"] = label.is_constructive
        else:
            # Fallback: we still know if Steam marked the review up or down.
            review["sentiment"] = "positive" if review.get("voted_up") else "negative"
            review["category"] = "other"
            review["is_constructive"] = True   # don't filter what we are unsure about


def classify_all(client, reviews: list, on_progress=None, context: str = "") -> list:
    """
    Classify every review and attach sentiment + category + is_constructive.

    Batches are sent CONCURRENTLY (a thread pool of MAX_WORKERS). Each batch just
    waits on the API, so overlapping them collapses many sequential calls into
    roughly one call's worth of wall-time. Reviews are labeled in place, so the
    original order is preserved.

    Returns:
        The same reviews list, each enriched with classification fields.
    """
    total = len(reviews)
    if total == 0:
        return reviews

    batches = [reviews[s:s + BATCH_SIZE] for s in range(0, total, BATCH_SIZE)]
    total_batches = len(batches)
    print(f"Classifying {total} reviews in {total_batches} batches "
          f"({MAX_WORKERS} at a time)...")

    done = 0
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(_classify_one, client, b, context) for b in batches]
        for fut in as_completed(futures):
            fut.result()   # surface any unexpected error
            with lock:
                done += 1
                if on_progress:
                    on_progress(done / total_batches)

    return reviews


def hybrid_classify_all(client, reviews: list, on_progress=None, context: str = "") -> list:
    """
    Classify with the distilled local models where confident, LLM otherwise.

    Every review is embedded once here and the vector stashed on it (r["_embedding"])
    so the theming step can reuse it. Confident reviews are labeled locally (no API
    call); the rest fall back to the batched LLM classifier. Only LLM-labeled
    reviews should be logged for future retraining (see pipeline).
    """
    if not reviews:
        return reviews
    from cluster_themes import embed_texts
    import local_classify

    embs = embed_texts([r.get("text", "") for r in reviews])
    for r, e in zip(reviews, embs):
        r["_embedding"] = e
    if on_progress:
        on_progress(0.3)

    uncertain = reviews
    if local_classify.available():
        preds = local_classify.classify(embs)
        uncertain = []
        for r, p in zip(reviews, preds or []):
            if p and p["confident"]:
                r["sentiment"] = p["sentiment"]
                r["category"] = p["category"]
                r["is_constructive"] = p["is_constructive"]
                r["_llm_labeled"] = False
            else:
                uncertain.append(r)
        print(f"Local-classified {len(reviews) - len(uncertain)}/{len(reviews)}; "
              f"LLM fallback for {len(uncertain)}.")

    if uncertain:
        classify_all(client, uncertain,
                     on_progress=(lambda f: on_progress(0.3 + f * 0.7)) if on_progress else None,
                     context=context)
        for r in uncertain:
            r["_llm_labeled"] = True
    elif on_progress:
        on_progress(1.0)
    return reviews


def save_classified(reviews: list, source_file: str) -> str:
    """
    Save the classified reviews next to the source, prefixed with 'classified_'.

    Args:
        reviews:     The enriched review list.
        source_file: Path to the original reviews file.

    Returns:
        The path written.
    """
    folder = os.path.dirname(source_file) or "."
    base = os.path.basename(source_file)
    out_path = os.path.join(folder, f"classified_{base}")

    clean = [{k: v for k, v in r.items() if k != "_embedding"} for r in reviews]
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)

    return out_path


# ----------------------------------------------------------------------------
# Command-line entry point
# ----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Classify a whole reviews file in batches and save the result."
    )
    parser.add_argument(
        "data_file",
        nargs="?",
        default="data/reviews_730_negative.json",
        help="Path to a reviews JSON file (default: data/reviews_730_negative.json)",
    )
    args = parser.parse_args()

    with open(args.data_file, encoding="utf-8") as f:
        reviews = json.load(f)

    print(f"Loaded {len(reviews)} reviews from {args.data_file}\n")

    client = get_client()
    enriched = classify_all(client, reviews)

    out_path = save_classified(enriched, args.data_file)

    # Quick summary: how many landed in each category.
    counts = {}
    for r in enriched:
        counts[r["category"]] = counts.get(r["category"], 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)

    print(f"\nDone. Saved {len(enriched)} classified reviews to {out_path}")
    print("Category breakdown:")
    for category, count in ordered:
        print(f"  {category:<13} {count}")


if __name__ == "__main__":
    main()
