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
import re
import time

from typing import Literal

from pydantic import BaseModel

# Reuse our client/helper, the category type, and the batching settings.
from llm import get_client, generate_json
from classify import ReviewCategory
from concurrent.futures import ThreadPoolExecutor, as_completed
from classify_batch import BATCH_SIZE, DELAY_BETWEEN_BATCHES, MAX_REVIEW_CHARS, MAX_WORKERS


# ----------------------------------------------------------------------------
# Tuning knobs
# ----------------------------------------------------------------------------

SAMPLE_FOR_DISCOVERY = 120     # how many reviews to show the discovery pass
TARGET_THEME_COUNT = "8 to 12" # how many themes we ask the model to produce
EXAMPLES_PER_THEME = 4         # representative quotes to keep per theme (filterable)
UNCLEAR_LABEL = "unclear"      # fallback when a review fits no discovered theme

# Theming method:
#   "llm"   assign every review with the LLM (richest themes; best for small sets)
#   "embed" embeddings + k-means clustering (bounded cost; best at large scale)
#   "auto"  pick per game: llm when few constructive reviews, embed when many
# Embed falls back to llm automatically if it is unavailable or errors.
THEME_METHOD = os.environ.get("THEME_METHOD", "llm").lower()
AUTO_EMBED_MIN = int(os.environ.get("AUTO_EMBED_MIN", "250"))  # constructive-review
                                # threshold at which "auto" switches llm -> embed
MIN_THEME_COUNT = int(os.environ.get("MIN_THEME_COUNT", "2"))
                                # a theme needs at least this many reviews to count
                                # as recurring; below it we drop it, so thin negative
                                # pools stop "clawing at straws" with 1-review themes


# ----------------------------------------------------------------------------
# Schemas
# ----------------------------------------------------------------------------

class ThemeDef(BaseModel):
    """A theme proposed during the discovery pass."""
    name: str               # short, specific, e.g. "blatant cheaters in Premier"
    description: str         # one-line explanation
    category: ReviewCategory
    # "feature": a specific, actionable aspect devs could act on.
    # "emotional": general sentiment / mood / nostalgia, nothing to act on.
    kind: Literal["feature", "emotional"]


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


def discover_themes(client, reviews: list, target: str = TARGET_THEME_COUNT) -> list:
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
        f"Identify {target} SPECIFIC, recurring themes across these "
        "reviews. Each theme should be concrete and actionable, not a vague "
        "category. For example, prefer 'blatant cheaters in Premier mode' over "
        "just 'cheating'.\n\n"
        "For each theme give a short name (3-6 words), a one-line description, "
        "the category it belongs to, and a 'kind': use \"feature\" if the theme "
        "is about a specific, actionable aspect of the game (a system, mechanic, "
        "feature, bug, or piece of content) that the developers could act on, or "
        "\"emotional\" if it is general sentiment, mood, nostalgia, or a "
        "farewell with nothing specific to act on.\n\n"
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


def _assign_one(client, batch: list, themes: list, canonical: dict) -> None:
    """Assign one batch of reviews to themes, writing 'theme' onto each in place."""
    prompt = build_assignment_prompt([r["text"] for r in batch], themes)

    # One retry on failure (e.g. a rate-limit blip), mirroring the classifier.
    results = []
    for attempt in range(1, 3):
        try:
            results = generate_json(client, prompt, list[ThemeAssignment]) or []
            break
        except Exception as err:
            print(f"  Assign batch failed (attempt {attempt}): {err}")
            time.sleep(DELAY_BETWEEN_BATCHES * attempt)

    by_index = {a.index: a for a in results}
    for i, review in enumerate(batch):
        assignment = by_index.get(i)
        review["theme"] = (canonical.get(assignment.theme.lower(), UNCLEAR_LABEL)
                           if assignment is not None else UNCLEAR_LABEL)


def assign_themes(client, reviews: list, themes: list) -> list:
    """
    Label every review with one of the discovered themes.

    Assignment batches run CONCURRENTLY (a thread pool of MAX_WORKERS), since each
    is just an independent API call. The model returns theme names as free text,
    so we normalize each back to a known theme; anything unrecognized becomes
    'unclear'. Reviews are labeled in place, preserving order.

    Returns:
        The same reviews, each with a new 'theme' field.
    """
    # Lookup table: lowercase theme name -> canonical name.
    canonical = {t.name.lower(): t.name for t in themes}

    batches = [reviews[s:s + BATCH_SIZE] for s in range(0, len(reviews), BATCH_SIZE)]
    print(f"Assigning {len(reviews)} reviews to themes in {len(batches)} batches "
          f"({MAX_WORKERS} at a time)...")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(_assign_one, client, b, themes, canonical) for b in batches]
        for fut in as_completed(futures):
            fut.result()

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


def build_sentiment_timeline(classified: list) -> dict:
    """
    Bucket classified reviews by date into a sentiment-over-time series.

    Granularity adapts to the data's span: daily for short spans, monthly for
    long ones, so the trend reads well for both busy and niche games.

    Returns:
        {"granularity": "day"|"month",
         "points": [{"label", "positive", "negative", "neutral"}, ...]}
        ordered chronologically. Empty points if there is too little dated data.
    """
    from datetime import date as _date

    dated = [r for r in classified if r.get("created_date")]
    if len(dated) < 4:
        return {"granularity": "day", "points": []}

    # created_date is "YYYY-MM-DD"; the span decides the bucket size.
    days = sorted(r["created_date"] for r in dated)
    try:
        span_days = (_date.fromisoformat(days[-1]) - _date.fromisoformat(days[0])).days
    except ValueError:
        span_days = 0
    by_month = span_days > 45   # short spans stay daily; longer ones go monthly

    buckets = {}
    for r in dated:
        key = r["created_date"][:7] if by_month else r["created_date"]
        bucket = buckets.setdefault(key, {"positive": 0, "negative": 0, "neutral": 0})
        sentiment = r.get("sentiment", "neutral")
        if sentiment not in bucket:
            sentiment = "neutral"
        bucket[sentiment] += 1

    points = [dict(label=k, **buckets[k]) for k in sorted(buckets)]
    return {"granularity": "month" if by_month else "day", "points": points}


def _is_english(language) -> int:
    """1 if Steam tagged the review's language as English, else 0. Reliable across
    scripts (the old character heuristic mislabeled Turkish, Spanish, etc.)."""
    return 1 if (language or "").lower() == "english" else 0


_SNIPPET_STOP = set(
    "the a an and or of to in on for with is are was were be been it its this that "
    "these those you your they them their as at by from about into over under not no "
    "but if then so than can will just get got very really more most some any all my "
    "me we our he she his her out up down do does did has have had game games really".split()
)


def _signal_weights(name, description, category):
    """Weighted theme keywords for snippet scoring: the name matters most, then the
    description, then the (generic) category. Short words and stopwords are ignored."""
    weights = {}
    for text, w in ((description, 2), (category, 1), (name, 3)):
        for tok in re.findall(r"[a-z0-9]+", (text or "").lower()):
            if len(tok) >= 3 and tok not in _SNIPPET_STOP:
                weights[tok] = max(weights.get(tok, 0), w)
    return weights


def relevant_snippet(text, name="", description="", category="", width=300):
    """
    Return the ~width-char slice of a review most about the given theme, instead of
    always its opening (long reviews often lead with unrelated chatter before getting
    to the point). Scores each sentence by weighted overlap with the theme's words;
    falls back to the opening when nothing matches. Excerpts are marked with an
    ellipsis so it is clear the preview is not the start of the review.
    """
    text = (text or "").strip()
    if len(text) <= width:
        return text
    weights = _signal_weights(name, description, category)
    best_start, best_score = 0, 0
    if weights:
        for m in re.finditer(r"[^.!?\n]+", text):
            toks = set(re.findall(r"[a-z0-9]+", m.group(0).lower()))
            score = sum(weights[tok] for tok in toks if tok in weights)
            if score > best_score:
                best_score, best_start = score, m.start()
    if not best_score:
        return text[:width].rstrip()
    snippet = text[best_start:best_start + width]
    tail = best_start + width < len(text)
    if tail:                      # avoid cutting the last word mid-way
        cut = snippet.rfind(" ")
        if cut > width * 0.6:
            snippet = snippet[:cut]
    snippet = snippet.strip()
    prefix = "\u2026 " if best_start > 0 else ""
    suffix = " \u2026" if tail else ""
    return prefix + snippet + suffix


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
    meta = {t.name: {"description": t.description, "category": t.category, "kind": t.kind} for t in themes}

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
        # Pick example quotes: the most helpful overall, but guarantee up to two
        # English ones if the theme has any, so the English-only review filter
        # still has quotes to show on majority-foreign games.
        english_items = [r for r in items_sorted if _is_english(r.get("language"))]
        picked, seen = [], set()
        for r in items_sorted[:2] + english_items[:2] + items_sorted:
            key = (r.get("steamid"), r.get("text"))
            if key in seen:
                continue
            seen.add(key)
            picked.append(r)
            if len(picked) >= EXAMPLES_PER_THEME:
                break
        info = meta.get(theme_name, {"description": "", "category": "other", "kind": "feature"})
        examples = [
            {
                "text": relevant_snippet(r["text"], theme_name, info["description"], info["category"]),
                "helpful_votes": r.get("helpful_votes", 0),
                "playtime_at_review_hours": r.get("playtime_at_review_hours", 0),
                "steamid": r.get("steamid"),
                "voted_up": r.get("voted_up"),
                "language": r.get("language"),
                "en": _is_english(r.get("language")),
            }
            for r in picked
        ]

        # Impact score: the summed weight of every review under this theme.
        impact = round(sum(review_impact(r) for r in items), 1)

        records.append({
            "theme": theme_name,
            "category": info["category"],
            "description": info["description"],
            "kind": info.get("kind", "feature"),
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
                    "steamid": r.get("steamid"),
                    "voted_up": r.get("voted_up"),
                }
                for r in noise_sorted[:EXAMPLES_PER_THEME]
            ],
        })

    return records, len(reviews), len(noise), themes


def _target_theme_count(n: int) -> str:
    """Scale how many themes to request to the pool size, so a thin pool is not
    padded with invented, single-review 'themes'."""
    if n < 15:
        return "2 to 4"
    if n < 40:
        return "3 to 6"
    if n < 120:
        return "5 to 9"
    return "8 to 12"


def _route_side(r: dict) -> str:
    """
    Decide whether a constructive review belongs on the Issues ('neg') or Praise
    ('pos') side, trusting the player's own recommend vote first and using the
    model's sentiment to refine it:

      - Thumbs-down (voted_up False)  -> Issues (the player did not recommend it).
      - Otherwise (thumbs-up, or the flag is missing) -> Issues only if the text is
        clearly negative (a fan naming a real problem); everything else -> Praise,
        so lukewarm/neutral recommendations stop padding the Issues side.
    """
    if r.get('voted_up') is False:
        return 'neg'
    return 'neg' if r.get('sentiment') == 'negative' else 'pos'


def analyze_both(client, classified: list, on_progress=None) -> dict:
    """
    Theme negative-leaning and positive-leaning reviews SEPARATELY, from a single
    classified pass, so the report can show both "Fix These" and "Double Down".

    Returns:
      {
        "negative": [theme records],   # Issues: not-recommended, or recommended-but-negative
        "positive": [theme records],   # Praise: recommended / positive-leaning reviews
        "noise": {"count": int, "examples": [...]},
        "sentiment_totals": {"positive", "negative", "neutral"},  # all reviews
        "total_reviews": int,
      }
    """
    noise = [r for r in classified if not r.get("is_constructive", True)]
    constructive = [r for r in classified if r.get("is_constructive", True)]
    negative = [r for r in constructive if _route_side(r) == "neg"]
    positive = [r for r in constructive if _route_side(r) == "pos"]

    # Resolve "auto" once per report: embed only pays off when there are enough
    # constructive reviews; below the threshold the LLM path is faster and richer.
    method = THEME_METHOD
    if method == "auto":
        method = "embed" if len(constructive) >= AUTO_EMBED_MIN else "llm"
        print(f"Auto theming: {len(constructive)} constructive reviews -> {method}")

    def theme_group(group, label):
        if not group:
            return []
        print(f"Theming {label} reviews ({len(group)}) via {method}...")
        records = None
        if method == "embed":
            try:
                from cluster_themes import theme_group_embed
                records = theme_group_embed(client, group)
            except Exception as err:
                print(f"  Embedding theming failed ({err}); using LLM theming.")
        if records is None:
            # Ask for fewer themes on a small pool so we do not invent straws.
            themes = discover_themes(client, group, _target_theme_count(len(group)))
            assign_themes(client, group, themes)
            records = aggregate_themes(group, themes)
        # A theme needs recurrence: drop any backed by fewer than MIN_THEME_COUNT reviews.
        return [rec for rec in records if rec.get('count', 0) >= MIN_THEME_COUNT]

    if on_progress:
        on_progress(0.0, "Finding what players want fixed")
    negative_records = theme_group(negative, "negative")
    if on_progress:
        on_progress(0.55, "Finding what players love")
    positive_records = theme_group(positive, "positive")
    if on_progress:
        on_progress(1.0, "Finalizing report")

    noise_sorted = sorted(
        noise,
        key=lambda r: (r.get("helpful_votes", 0), r.get("playtime_at_review_hours", 0)),
        reverse=True,
    )
    noise_summary = {
        "count": len(noise),
        "examples": [
            {
                "text": r["text"][:300],
                "helpful_votes": r.get("helpful_votes", 0),
                "playtime_at_review_hours": r.get("playtime_at_review_hours", 0),
                "steamid": r.get("steamid"),
                "voted_up": r.get("voted_up"),
                "en": _is_english(r.get("language")),
            }
            for r in noise_sorted[:EXAMPLES_PER_THEME]
        ],
    }

    # Compact per-review array so the report can recompute the dashboard live
    # under filters (playtime / recommend / language) without re-analyzing.
    reviews_compact = []
    for r in classified:
        co = 1 if r.get("is_constructive", True) else 0
        theme = r.get("theme", "") if co else ""
        if theme in (UNCLEAR_LABEL, "noise", None):
            theme = ""
        side = _route_side(r) if co else ""
        reviews_compact.append({
            "co": co,                                  # constructive (1) vs noise (0)
            "sd": side,                                # 'neg' / 'pos' / ''
            "th": theme or "",                         # theme name ('' = unclear/noise)
            "ca": r.get("category", "other"),
            "se": r.get("sentiment", "neutral"),
            "pt": round(r.get("playtime_at_review_hours", 0) or 0, 1),
            "hv": r.get("helpful_votes", 0) or 0,
            "vu": 1 if r.get("voted_up") else 0,
            "en": _is_english(r.get("language")),
            "dt": r.get("created_date") or "",
        })

    return {
        "negative": negative_records,
        "positive": positive_records,
        "noise": noise_summary,
        "sentiment_totals": _count_sentiments(classified),
        "sentiment_timeline": build_sentiment_timeline(classified),
        "total_reviews": len(classified),
        "reviews": reviews_compact,
    }


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
