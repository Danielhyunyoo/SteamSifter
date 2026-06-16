"""
pipeline.py

One-command orchestrator with per-game caching.

Running the four steps by hand (fetch, classify, themes, report) is tedious and,
more importantly, re-spends API quota every time. This script runs the whole
pipeline for a single game and CACHES the result: if a game was analyzed
recently, it skips straight to regenerating the (free) report instead of calling
the model again.

This is the piece a future web app would call: the hundredth person to look up a
popular game pays zero API cost because the analysis is already cached.

Examples:
    python src/pipeline.py 730 --title "Counter-Strike 2"
    python src/pipeline.py 1085660 --type positive --title "Destiny 2"
    python src/pipeline.py 730 --refresh        # force a fresh re-analysis
    python src/pipeline.py 730 --max-age 1       # treat cache older than 1 day as stale
"""

import argparse
import json
import os
import time

from llm import get_client
from fetch_reviews import fetch_reviews, save_reviews
from classify_batch import classify_all, save_classified
from themes import analyze_reviews
from report import build_html


DATA_DIR = "data"
DEFAULT_MAX_AGE_DAYS = 7   # reviews drift over time, so caches expire


def cache_paths(app_id: str, review_type: str) -> dict:
    """Return the on-disk paths used for this game + review type."""
    suffix = f"{app_id}_{review_type}"
    return {
        "reviews": os.path.join(DATA_DIR, f"reviews_{suffix}.json"),
        "classified": os.path.join(DATA_DIR, f"classified_reviews_{suffix}.json"),
        "themes": os.path.join(DATA_DIR, f"themes_reviews_{suffix}.json"),
    }


def is_fresh(path: str, max_age_days: float) -> bool:
    """True if the file exists and is newer than max_age_days."""
    if not os.path.exists(path):
        return False
    age_seconds = time.time() - os.path.getmtime(path)
    return age_seconds < max_age_days * 86400


def cache_age_days(path: str) -> float:
    """How many days old the cached file is."""
    return (time.time() - os.path.getmtime(path)) / 86400


def get_records(app_id: str, review_type: str = "negative", max_reviews: int = 300,
                refresh: bool = False, max_age_days: float = DEFAULT_MAX_AGE_DAYS) -> list:
    """
    Return the theme records for a game: from cache if fresh and readable,
    otherwise by running the full analysis (fetch -> classify -> theme) and
    caching the result. This is the reusable core the web app calls.
    """
    paths = cache_paths(app_id, review_type)

    # ---- Cache check: reuse the themes result if it is fresh AND readable ----
    if not refresh and is_fresh(paths["themes"], max_age_days):
        try:
            with open(paths["themes"], encoding="utf-8") as f:
                records = json.load(f)
            print(f"Cache HIT for app {app_id} ({review_type}). Reusing analysis "
                  f"from {cache_age_days(paths['themes']):.1f} day(s) ago. No API calls needed.")
            return records
        except (json.JSONDecodeError, OSError) as err:
            print(f"Cached analysis was unreadable ({err}); re-analyzing.")

    # ---- Rebuild ----
    reason = "refresh requested" if refresh else "no fresh/usable cache"
    print(f"Cache MISS for app {app_id} ({review_type}): {reason}. Running analysis...")
    client = get_client()

    # Step 1: fetch reviews (reuse the saved file unless refreshing).
    if refresh or not os.path.exists(paths["reviews"]):
        print(f"Fetching up to {max_reviews} '{review_type}' reviews...")
        reviews = fetch_reviews(app_id, max_reviews=max_reviews, review_type=review_type)
        save_reviews(reviews, app_id, review_type)
    else:
        print(f"Reusing fetched reviews from {paths['reviews']}")
        with open(paths["reviews"], encoding="utf-8") as f:
            reviews = json.load(f)

    # Step 2: classify (sentiment, category, is_constructive).
    print("Classifying reviews...")
    classified = classify_all(client, reviews)
    save_classified(classified, paths["reviews"])

    # Step 3: theme + rank.
    print("Discovering and assigning themes...")
    records, n_constructive, n_noise, themes = analyze_reviews(client, classified)
    print(f"  Constructive: {n_constructive}  |  Noise: {n_noise}  |  Themes: {len(themes)}")

    # Save the themes result (this IS the cache for next time).
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(paths["themes"], "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    return records


def run(app_id: str, review_type: str = "negative", mode: str = None,
        title: str = None, max_reviews: int = 300, refresh: bool = False,
        max_age_days: float = DEFAULT_MAX_AGE_DAYS, out: str = "steamsifter_report.html") -> str:
    """
    Run (or reuse a cached) analysis for one game, then write the HTML report.
    Returns the path of the report that was written.
    """
    mode = mode or ("positive" if review_type == "positive" else "negative")
    title = title or f"App {app_id}"

    records = get_records(app_id, review_type, max_reviews=max_reviews,
                          refresh=refresh, max_age_days=max_age_days)

    html_doc = build_html(records, title, mode)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html_doc)

    print(f"Report written to {os.path.abspath(out)}")
    return out


def main():
    parser = argparse.ArgumentParser(
        description="Run the full SteamSifter pipeline for one game, with caching."
    )
    parser.add_argument("app_id", help="Steam application ID, e.g. 730")
    parser.add_argument("--type", default="negative",
                        choices=["all", "positive", "negative"],
                        help="Which reviews to analyze (default: negative)")
    parser.add_argument("--mode", default=None, choices=["negative", "positive"],
                        help="Report framing; defaults to match --type")
    parser.add_argument("--title", default=None, help="Game title for the report header")
    parser.add_argument("--max", type=int, default=300, help="Max reviews to fetch")
    parser.add_argument("--refresh", action="store_true",
                        help="Ignore the cache and re-run the analysis from scratch")
    parser.add_argument("--max-age", type=float, default=DEFAULT_MAX_AGE_DAYS,
                        help="Treat cached analysis older than this many days as stale")
    parser.add_argument("--out", default="steamsifter_report.html",
                        help="Output HTML report path")
    args = parser.parse_args()

    run(args.app_id, review_type=args.type, mode=args.mode, title=args.title,
        max_reviews=args.max, refresh=args.refresh, max_age_days=args.max_age,
        out=args.out)


if __name__ == "__main__":
    main()
