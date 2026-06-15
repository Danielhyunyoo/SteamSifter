"""
themes.py

Turns classified reviews into specific, named THEMES.

A category like "cheating" is too coarse to act on. This step breaks categories
down into concrete recurring themes such as "blatant cheaters in Premier" or
"rampant bots in casual", then counts how many reviews fall under each.

It works in two passes:
  1. DISCOVER: read a sample of reviews and propose a short list of specific,
     named themes (each tied to a category).
  2. ASSIGN:   label every review with the single best theme from that list,
     in batches, which gives us exact counts instead of estimates.

Run directly on a classified file:
    python src/themes.py data/classified_reviews_730_negative.json
"""

import argparse
import json
import math
import os
import time

from pydantic import BaseModel

# Reuse our client/helper, the category type, and the batching settings.
from llm import get_client, generate_json
from classify import ReviewCategory
from classify_batch import BATCH_SIZE, DELAY_BETWEEN_BATCHES, MAX_REVIEW_CHARS


# ----------------------------------------------------------------------------
# Tuning knobs
# ----------------------------------------------------------------------------

SAMPLE_FOR_DISCOVERY = 120     # how many reviews to show the discovery pass
TARGET_THEME_COUNT = "8 to 12" # how many themes we ask the model to produce
EXAMPLES_PER_THEME = 2         # representative quotes to keep per theme
UNCLEAR_LABEL = "unclear"      # fallback when a review fits no discovered theme


# ----------------------------------------------------------------------------
# Schemas
# ----------------------------------------------------------------------------

class ThemeDef(BaseModel):
    """A theme proposed during the discovery pass."""
    name: str               # short, specific, e.g. "blatant cheaters in Premier"
    description: str         # one-line explanation
    category: ReviewCategory


class ThemeAssignment(BaseModel):
    """One review's theme assignment within a batch."""
    index: int              # position within the batch prompt
    theme: str              # must match a discovered theme name (we normalize)


# ----------------------------------------------------------------------------
# Pass 1: discover themes from a sample
# ----------------------------------------------------------------------------

def sample_reviews(reviews: list, sample_size: int) -> list:
    """
    Take an evenly-spaced sample across the whole list so the discovery pass
    sees a representative spread, not just the first N reviews.
    """
    if len(reviews) <= sample_size:
        return reviews
    stride = len(reviews) // sample_size
    return [reviews[i] for i in range(0, len(reviews), stride)][:sample_size]


def discover_themes(client, reviews: list) -> list:
    """
    Ask the model to propose a list of specific, named themes from a sample.

    Returns:
        A list of ThemeDef objects.
    """
    sample = sample_reviews(reviews, SAMPLE_FOR_DISCOVERY)

    # Show each sampled review with its category for context.
    lines = []
    for r in sample:
        snippet = (r.get("text") or "").strip()[:MAX_REVIEW_CHARS]
        category = r.get("category", "other")
        lines.append(f"[{category}] {snippet}")
    reviews_block = "\n".join(lines)

    prompt = (
        "Below is a sample of player reviews for one video game on Steam, each "
        "tagged with a rough category.\n\n"
        f"Identify {TARGET_THEME_COUNT} SPECIFIC, recurring themes across these "
        "reviews. Each theme should be concrete and actionable, not a vague "
        "category. For example, prefer 'blatant cheaters in Premier mode' over "
        "just 'cheating'.\n\n"
        "For each theme give a short name (3-6 words), a one-line description, "
        "and the category it belongs to.\n\n"
        f"Reviews:\n{reviews_block}"
    )

    themes = generate_json(client, prompt, list[ThemeDef])
    return themes or []


# ----------------------------------------------------------------------------
# Pass 2: assign every review to one of the discovered themes
# ----------------------------------------------------------------------------

def build_assignment_prompt(texts: list, themes: list) -> str:
    """
    Build a prompt that lists the discovered themes, then a batch of reviews,
    asking the model to label each review with exactly one theme name.
    """
    theme_lines = [f"- {t.name}: {t.description}" for t in themes]
    themes_block = "\n".join(theme_lines)

    numbered = []
    for i, text in enumerate(texts):
        snippet = (text or "").strip()[:MAX_REVIEW_CHARS]
        numbered.append(f"Review {i}:\n{snippet}")
    reviews_block = "\n\n".join(numbered)

    return (
        "Here is a fixed list of themes:\n"
        f"{themes_block}\n\n"
        "Assign EACH review below to the single best-matching theme. Use the "
        "theme's exact name. If a review fits none of them (e.g. it is a joke, "
        f"empty, or off-topic), use \"{UNCLEAR_LABEL}\".\n\n"
        f"{reviews_block}"
    )


def assign_themes(client, reviews: list, themes: list) -> list:
    """
    Label every review with one of the discovered themes, in batches.

    The model returns theme names as free text, so we normalize each one back to
    a known theme (case-insensitive). Anything unrecognized becomes 'unclear'.

    Returns:
        The same reviews, each with a new 'theme' field.
    """
    # Lookup table: lowercase theme name -> canonical name.
    canonical = {t.name.lower(): t.name for t in themes}

    total = len(reviews)
    total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE

    for start in range(0, total, BATCH_SIZE):
        batch = reviews[start:start + BATCH_SIZE]
        texts = [r["text"] for r in batch]

        batch_num = (start // BATCH_SIZE) + 1
        print(f"Assigning themes, batch {batch_num}/{total_batches} "
              f"(reviews {start + 1}-{start + len(batch)})...")

        prompt = build_assignment_prompt(texts, themes)

        # One retry on failure (e.g. rate limit), mirroring the classifier.
        results = []
        for attempt in range(1, 3):
            try:
                results = generate_json(client, prompt, list[ThemeAssignment]) or []
                break
            except Exception as err:
                print(f"  Batch failed (attempt {attempt}): {err}")
                time.sleep(DELAY_BETWEEN_BATCHES * attempt)

        by_index = {a.index: a for a in results}

        # Attach a normalized theme name to each review in the batch.
        for i, review in enumerate(batch):
            assignment = by_index.get(i)
            if assignment is not None:
                review["theme"] = canonical.get(assignment.theme.lower(), UNCLEAR_LABEL)
            else:
                review["theme"] = UNCLEAR_LABEL

        if start + BATCH_SIZE < total:
            time.sleep(DELAY_BETWEEN_BATCHES)

    return reviews


# ----------------------------------------------------------------------------
# Aggregate: count reviews per theme and pick representative examples
# ----------------------------------------------------------------------------

def review_impact(review: dict) -> float:
    """
    Estimate how much weight one review should carry.

    A review counts more when it comes from a credible source: someone with lots
    of playtime, and/or whose review the community marked helpful. We use a log
    scale so a handful of extreme outliers (10,000 hours, 5,000 upvotes) don't
    completely drown out everyone else.

    Baseline (0 hours, 0 helpful votes) = 1.0, so every review still counts once.
    """
    hours = review.get("playtime_at_review_hours", 0) or 0
    helpful = review.get("helpful_votes", 0) or 0
    return 1.0 + math.log10(1 + hours) + math.log10(1 + helpful)


def _count_sentiments(reviews: list) -> dict:
    """Tally positive/negative/neutral across a set of reviews."""
    counts = {"positive": 0, "negative": 0, "neutral": 0}
    for r in reviews:
        s = r.get("sentiment", "neutral")
        counts[s] = counts.get(s, 0) + 1
    return counts


def aggregate_themes(reviews: list, themes: list) -> list:
    """
    Build the final theme records: count, impact score, and example quotes.

    Themes are ranked by IMPACT, not raw count. Impact sums each review's weight
    (see review_impact), so a theme raised by experienced, upvoted players ranks
    above one raised by an equal number of low-effort drive-by reviews.

    Returns:
        A list of theme dicts sorted by impact score (highest first).
    """
    # Description/category lookup from the discovered themes.
    meta = {t.name: {"description": t.description, "category": t.category} for t in themes}

    # Bucket reviews by their assigned theme.
    buckets = {}
    for r in reviews:
        buckets.setdefault(r.get("theme", UNCLEAR_LABEL), []).append(r)

    records = []
    for theme_name, items in buckets.items():
        # Sort a theme's reviews by credibility for example selection.
        items_sorted = sorted(
            items,
            key=lambda r: (r.get("helpful_votes", 0), r.get("playtime_at_review_hours", 0)),
            reverse=True,
        )
        examples = [
            {
                "text": r["text"][:300],
                "helpful_votes": r.get("helpful_votes", 0),
                "playtime_at_review_hours": r.get("playtime_at_review_hours", 0),
            }
            for r in items_sorted[:EXAMPLES_PER_THEME]
        ]

        # Impact score: the summed weight of every review under this theme.
        impact = round(sum(review_impact(r) for r in items), 1)

        info = meta.get(theme_name, {"description": "", "category": "other"})
        records.append({
            "theme": theme_name,
            "category": info["category"],
            "description": info["description"],
            "count": len(items),
            "impact_score": impact,
            "sentiment_counts": _count_sentiments(items),
            "examples": examples,
        })

    # Highest-impact themes first.
    records.sort(key=lambda rec: rec["impact_score"], reverse=True)
    return records


def analyze_reviews(client, all_reviews: list):
    """
    Run the full theming analysis on a list of classified reviews.

    Splits noise from constructive feedback, discovers themes, assigns every
    constructive review to one, aggregates into ranked records, and appends a
    'noise' record so the report can show how much was filtered out.

    Returns:
        (records, num_constructive, num_noise, themes)
    """
    # Noise filter: only theme the constructive reviews. Default to constructive
    # for older files that lack the is_constructive flag.
    reviews = [r for r in all_reviews if r.get("is_constructive", True)]
    noise = [r for r in all_reviews if not r.get("is_constructive", True)]

    themes = discover_themes(client, reviews)
    reviews = assign_themes(client, reviews, themes)
    records = aggregate_themes(reviews, themes)

    # Append a 'noise' record summarizing what was filtered out.
    if noise:
        noise_sorted = sorted(
            noise,
            key=lambda r: (r.get("helpful_votes", 0), r.get("playtime_at_review_hours", 0)),
            reverse=True,
        )
        records.append({
            "theme": "noise",
            "category": "other",
            "description": "Low-signal reviews filtered out before theming: jokes, "
                           "one-liners, off-topic rants, and spam.",
            "count": len(noise),
            "impact_score": round(sum(review_impact(r) for r in noise), 1),
            "sentiment_counts": _count_sentiments(noise),
            "examples": [
                {
                    "text": r["text"][:300],
                    "helpful_votes": r.get("helpful_votes", 0),
                    "playtime_at_review_hours": r.get("playtime_at_review_hours", 0),
                }
                for r in noise_sorted[:EXAMPLES_PER_THEME]
            ],
        })

    return records, len(reviews), len(noise), themes


# ----------------------------------------------------------------------------
# Command-line entry point
# ----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Discover and assign themes for a classified reviews file."
    )
    parser.add_argument(
        "data_file",
        nargs="?",
        default="data/classified_reviews_730_negative.json",
        help="Path to a classified reviews JSON file",
    )
    args = parser.parse_args()

    with open(args.data_file, encoding="utf-8") as f:
        all_reviews = json.load(f)

    print(f"Loaded {len(all_reviews)} reviews from {args.data_file}")

    client = get_client()
    print("Discovering themes and assigning reviews (this calls the model)...")
    records, n_constructive, n_noise, themes = analyze_reviews(client, all_reviews)
    print(f"  Constructive: {n_constructive}  |  Noise filtered out: {n_noise}")
    print(f"  Themes found: {len(themes)}")

    folder = os.path.dirname(args.data_file) or "."
    base = os.path.basename(args.data_file).replace("classified_", "")
    out_path = os.path.join(folder, f"themes_{base}")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    # Print the ranked themes.
    print(f"\nSaved {len(records)} themes to {out_path}\n")
    print("Themes ranked by impact (review count in parentheses):")
    for rec in records:
        print(f"  impact {rec.get('impact_score', 0):>6}  ({rec['count']:>3})  "
              f"{rec['theme']}  [{rec['category']}]")


if __name__ == "__main__":
    main()
