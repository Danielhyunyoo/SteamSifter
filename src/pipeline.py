"""
pipeline.py

One-command orchestrator with per-game caching.

It fetches a game's reviews ONCE (all sentiments), classifies them once, then
themes the negative and positive sides separately, producing a single combined
analysis the report can toggle between. Results are cached per game, so repeat
lookups are instant and free.

This is the function the web app calls.

Examples:
    python src/pipeline.py 730 --title "Counter-Strike 2"
    python src/pipeline.py 730 --refresh
    python src/pipeline.py 730 --max-age 1
"""

import argparse
import json
import os
import time

from llm import get_client, generate_json
from pydantic import BaseModel
from fetch_reviews import fetch_reviews, save_reviews, fetch_review_total, fetch_player_summaries
from classify_batch import classify_all, save_classified
from themes import analyze_both
from report import build_html
import store


DATA_DIR = "data"
DEFAULT_MAX_AGE_DAYS = float(os.environ.get("MAX_AGE_DAYS", "30"))
                           # cache lifetime; a backstop since the 20% review-
                           # growth gate already refreshes active games on demand
DEFAULT_MAX_REVIEWS = int(os.environ.get("MAX_REVIEWS", "1000"))  # set higher to scale up


def cache_paths(app_id: str) -> dict:
    """On-disk paths for this game. Reviews are fetched as 'all' (both sides)."""
    return {
        "reviews": os.path.join(DATA_DIR, f"reviews_{app_id}_all.json"),
        "classified": os.path.join(DATA_DIR, f"classified_reviews_{app_id}_all.json"),
        "analysis": os.path.join(DATA_DIR, f"analysis_{app_id}.json"),
    }


def is_fresh(path: str, max_age_days: float) -> bool:
    """True if the file exists and is newer than max_age_days."""
    if not os.path.exists(path):
        return False
    return (time.time() - os.path.getmtime(path)) < max_age_days * 86400


def cache_age_days(path: str) -> float:
    """How many days old the cached file is."""
    return (time.time() - os.path.getmtime(path)) / 86400


def _attach_review_urls(analysis: dict, app_id: str) -> None:
    """Add a Steam review permalink to every example, from its steamid + app_id."""
    def add(examples):
        for ex in examples or []:
            sid = ex.get("steamid")
            if sid:
                ex["url"] = f"https://steamcommunity.com/profiles/{sid}/recommended/{app_id}/"
    for rec in analysis.get("negative", []):
        add(rec.get("examples"))
    for rec in analysis.get("positive", []):
        add(rec.get("examples"))
    add(analysis.get("noise", {}).get("examples"))


class _Translation(BaseModel):
    """One example quote translated to English (batched call below)."""
    index: int
    english: str


def _translate_batch(client, batch) -> None:
    """
    Translate any NON-English quote in one batch, in place.

    The model detects the language itself. We do NOT rely on Steam's language
    tag, which is the reviewer's client locale, not what they actually wrote (a
    Turkish review from an English client is tagged "english"), nor on a character
    heuristic, which misses Latin-script languages. English quotes are left as-is.
    """
    numbered = "\n".join(f"{i}: {ex.get('text', '')[:300]}" for i, ex in enumerate(batch))
    prompt = (
        "Below are numbered Steam game reviews. Some are written in English and "
        "some are not. For EVERY review that is NOT written in English, return its "
        "index and a natural, concise English translation. Do NOT return anything "
        "for reviews that are already in English.\n\n"
        f"{numbered}"
    )
    try:
        results = generate_json(client, prompt, list[_Translation]) or []
    except Exception as err:
        print(f"  Translation batch failed ({err}); leaving as-is.")
        return
    by_index = {r.index: r.english for r in results}
    for i, ex in enumerate(batch):
        english = (by_index.get(i) or "").strip()
        original = (ex.get("text", "") or "")[:300].strip()
        # Store only genuine translations (skip blanks and English echoed back).
        if english and english.lower() != original.lower():
            ex["translation"] = english


def _attach_translations(analysis: dict, client) -> None:
    """
    Add an English 'translation' to every non-English example quote.

    Quotes go to the model in batches and IT decides what is foreign, so anything
    non-English is caught regardless of script or the unreliable Steam tag.
    """
    examples = []
    for rec in analysis.get("negative", []) + analysis.get("positive", []):
        if rec.get("theme") in ("noise", "unclear"):
            continue
        examples.extend(rec.get("examples", []))
    if not examples:
        return
    for start in range(0, len(examples), 40):
        _translate_batch(client, examples[start:start + 40])


def _attach_authors(analysis: dict) -> None:
    """Attach each example reviewer's Steam avatar + username (best-effort)."""
    examples = []
    for rec in analysis.get("negative", []) + analysis.get("positive", []):
        if rec.get("theme") in ("noise", "unclear"):
            continue
        examples.extend(rec.get("examples", []))

    summaries = fetch_player_summaries([ex.get("steamid") for ex in examples])
    if not summaries:
        return
    for ex in examples:
        info = summaries.get(ex.get("steamid"))
        if info:
            ex["author_name"] = info["name"]
            ex["author_avatar"] = info["avatar"]


def get_analysis(app_id: str, max_reviews: int = DEFAULT_MAX_REVIEWS, refresh: bool = False,
                 max_age_days: float = DEFAULT_MAX_AGE_DAYS, progress=None) -> dict:
    """
    Return the combined analysis for a game (negative + positive themes), from
    cache if fresh and readable, otherwise by running the full pipeline and
    caching the result. This is the reusable core the web app calls.
    """
    paths = cache_paths(app_id)
    report = progress or (lambda pct, msg: None)

    # ---- Cache check: reuse the analysis from the store if fresh ----
    if not refresh:
        cached = store.load_analysis(app_id, max_age_days)
        if cached is not None:
            print(f"Cache HIT for app {app_id}. No API calls needed.")
            report(100, "Loaded from cache")
            return cached

    # ---- Rebuild ----
    reason = "refresh requested" if refresh else "no fresh/usable cache"
    print(f"Cache MISS for app {app_id}: {reason}. Running analysis...")
    client = get_client()

    # Step 1: fetch ALL reviews (reuse the saved file unless refreshing).
    report(3, "Fetching reviews")
    if refresh or not os.path.exists(paths["reviews"]):
        print(f"Fetching up to {max_reviews} reviews (all sentiments)...")
        reviews = fetch_reviews(app_id, max_reviews=max_reviews, review_type="all", language="all")
        save_reviews(reviews, app_id, "all")
    else:
        print(f"Reusing fetched reviews from {paths['reviews']}")
        with open(paths["reviews"], encoding="utf-8") as f:
            reviews = json.load(f)

    # Step 2: classify (sentiment, category, is_constructive). Maps to 10%-55%.
    report(10, "Classifying reviews")
    classified = classify_all(
        client, reviews,
        on_progress=lambda f: report(10 + int(f * 45), "Classifying reviews"),
    )
    save_classified(classified, paths["reviews"])

    # Step 3: theme negative and positive sides separately. Maps to 55%-98%.
    analysis = analyze_both(
        client, classified,
        on_progress=lambda f, msg: report(55 + int(f * 43), msg),
    )

    # Passively collect (review text, LLM label) samples for future distillation
    # of a fast local classifier. Best-effort; never blocks the analysis.
    try:
        import training_data
        training_data.log_samples(classified)
    except Exception as err:
        print(f"Training-data logging skipped ({err}).")

    # Attach each example review's Steam permalink so users can verify it is real.
    _attach_review_urls(analysis, app_id)

    # Translate any foreign-language example quotes to English (best-effort).
    try:
        _attach_translations(analysis, client)
    except Exception as err:
        print(f"Translation step failed ({err}); continuing without translations.")

    # Attach reviewer avatar + username so identical quotes read as distinct people.
    try:
        _attach_authors(analysis)
    except Exception as err:
        print(f"Author lookup failed ({err}); continuing without avatars.")

    # Record the game's true total review count, for the review-growth refresh gate.
    analysis["steam_total_reviews"] = fetch_review_total(app_id) or analysis.get("total_reviews", 0)

    report(100, "Done")

    # Persist the analysis (Redis if configured, else a local file).
    store.save_analysis(app_id, analysis, max_age_days)

    return analysis


def run(app_id: str, title: str = None, max_reviews: int = DEFAULT_MAX_REVIEWS, refresh: bool = False,
        max_age_days: float = DEFAULT_MAX_AGE_DAYS, out: str = "steamsifter_report.html") -> str:
    """Run (or reuse) the analysis and write the combined HTML report."""
    title = title or f"App {app_id}"
    analysis = get_analysis(app_id, max_reviews=max_reviews, refresh=refresh,
                            max_age_days=max_age_days)
    with open(out, "w", encoding="utf-8") as f:
        f.write(build_html(analysis, title))
    print(f"Report written to {os.path.abspath(out)}")
    return out


def main():
    parser = argparse.ArgumentParser(
        description="Run the full SteamSifter pipeline for one game, with caching."
    )
    parser.add_argument("app_id", help="Steam application ID, e.g. 730")
    parser.add_argument("--title", default=None, help="Game title for the report header")
    parser.add_argument("--max", type=int, default=300, help="Max reviews to fetch")
    parser.add_argument("--refresh", action="store_true",
                        help="Ignore the cache and re-run from scratch")
    parser.add_argument("--max-age", type=float, default=DEFAULT_MAX_AGE_DAYS,
                        help="Treat cached analysis older than this many days as stale")
    parser.add_argument("--out", default="steamsifter_report.html", help="Output HTML path")
    args = parser.parse_args()

    run(args.app_id, title=args.title, max_reviews=args.max,
        refresh=args.refresh, max_age_days=args.max_age, out=args.out)


if __name__ == "__main__":
    main()
