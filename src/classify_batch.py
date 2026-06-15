"""
classify_batch.py

Classifies a whole file of reviews efficiently, in batches.

Why batch? The free Gemini tier has a limited number of requests per minute.
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
from typing import Literal

from pydantic import BaseModel

# Reuse the client/helper and the category list we already defined.
from llm import get_client, generate_json
from classify import ReviewCategory


# ----------------------------------------------------------------------------
# Tuning knobs
# ----------------------------------------------------------------------------

BATCH_SIZE = 50                 # reviews sent per API call (bigger = fewer calls,
                                # which matters a lot given low free-tier daily quotas)
DELAY_BETWEEN_BATCHES = 4.5     # seconds to wait between calls (stay under ~15 RPM)
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

def build_batch_prompt(texts: list) -> str:
    """
    Build a single prompt that lists several reviews, each with its batch index.

    Args:
        texts: The review texts for this batch.

    Returns:
        A prompt string asking Gemini to classify every listed review.
    """
    # Number each review so the model can reference it by index in its answer.
    numbered = []
    for i, text in enumerate(texts):
        snippet = (text or "").strip()[:MAX_REVIEW_CHARS]
        numbered.append(f"Review {i}:\n{snippet}")

    reviews_block = "\n\n".join(numbered)

    return (
        "You are classifying players' reviews of a video game on Steam.\n"
        "For EACH review below, return its index, sentiment "
        "(positive/negative/neutral), the single best category from:\n"
        "  bug, performance, gameplay, cheating, community, monetization, "
        "content, ui_ux, praise, other,\n"
        "and is_constructive: true if the review gives real, on-topic feedback "
        "about the game; false if it is noise (a joke, meme, one-word or empty "
        "review, off-topic rant, or review-bomb spam with no useful content).\n\n"
        "Return one classification object per review.\n\n"
        f"{reviews_block}"
    )


# ----------------------------------------------------------------------------
# Classifying one batch (with retry on failure)
# ----------------------------------------------------------------------------

def classify_batch(client, texts: list) -> list:
    """
    Classify one batch of review texts, retrying if the call fails.

    Args:
        client: A genai.Client.
        texts:  The review texts for this batch.

    Returns:
        A list of BatchClassification objects (possibly fewer than len(texts)
        if the model omits some; the caller fills any gaps).
    """
    prompt = build_batch_prompt(texts)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            # response_schema as a list[...] makes Gemini return a JSON array.
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

def classify_all(client, reviews: list) -> list:
    """
    Classify every review, in batches, and attach sentiment + category to each.

    Args:
        client:  A genai.Client.
        reviews: The list of review dicts (from the fetch step).

    Returns:
        The same reviews, each with new 'sentiment' and 'category' fields added.
    """
    total = len(reviews)
    enriched = []

    # Walk through the reviews in chunks of BATCH_SIZE.
    for start in range(0, total, BATCH_SIZE):
        batch = reviews[start:start + BATCH_SIZE]
        texts = [r["text"] for r in batch]

        batch_num = (start // BATCH_SIZE) + 1
        total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
        print(f"Classifying batch {batch_num}/{total_batches} "
              f"(reviews {start + 1}-{start + len(batch)})...")

        results = classify_batch(client, texts)

        # Index the model's answers by their batch index for easy lookup.
        by_index = {r.index: r for r in results}

        # Attach the classification to each review. If the model skipped one,
        # fall back to a neutral/other label and the known voted_up sentiment.
        for i, review in enumerate(batch):
            label = by_index.get(i)
            if label is not None:
                review["sentiment"] = label.sentiment
                review["category"] = label.category
                review["is_constructive"] = label.is_constructive
            else:
                # Fallback: we still know if Steam marked it up or down.
                review["sentiment"] = "positive" if review.get("voted_up") else "negative"
                review["category"] = "other"
                review["is_constructive"] = True  # don't filter what we are unsure about
            enriched.append(review)

        # Pause between batches to respect the free-tier rate limit.
        if start + BATCH_SIZE < total:
            time.sleep(DELAY_BETWEEN_BATCHES)

    return enriched


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

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(reviews, f, ensure_ascii=False, indent=2)

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
