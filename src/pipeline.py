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
import re
import time

from llm import get_client, generate_json
from pydantic import BaseModel
from fetch_reviews import (fetch_reviews, fetch_reviews_balanced, save_reviews,
                           fetch_review_total, fetch_player_summaries, fetch_game_context,
                           fetch_app_header)
from concurrent.futures import ThreadPoolExecutor
from classify_batch import classify_all, hybrid_classify_all, save_classified, MAX_WORKERS
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


# Letters from scripts that are unmistakably not English (Greek, Cyrillic, Hebrew,
# Arabic, Devanagari, Thai, Kana, CJK, Hangul). Used as a safety net so a foreign
# quote is never left untranslated if the detect-and-translate pass skips it.
_NON_LATIN_RE = re.compile(
    "[\u0370-\u03ff\u0400-\u04ff\u0590-\u05ff\u0600-\u06ff\u0900-\u097f"
    "\u0e00-\u0e7f\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]"
)


def _looks_non_latin(text: str) -> bool:
    """True if the text clearly contains non-Latin-script letters (e.g. CJK)."""
    return len(_NON_LATIN_RE.findall(text or "")) >= 2


def _translate_batch(client, batch, force: bool = False) -> None:
    """
    Translate non-English quotes in one batch, in place.

    The model detects the language itself (we do NOT trust Steam's language tag,
    which is the reviewer's client locale, not what they wrote). Each review is
    flattened to a SINGLE line so newlines inside a review cannot break the numbered
    list and misalign the model's answers. With force=True every quote is translated
    unconditionally, used to sweep up anything the detection pass missed.
    """
    # One line per review: collapse internal whitespace so each "index: text" stays
    # on its own line for the model to parse reliably.
    numbered = "\n".join(
        f"{i}: {' '.join((ex.get('text', '') or '').split())[:300]}"
        for i, ex in enumerate(batch)
    )
    if force:
        prompt = (
            "Below are numbered non-English Steam game reviews. For EVERY index, "
            "return its index and a natural, concise English translation.\n\n"
            f"{numbered}"
        )
    else:
        prompt = (
            "Below are numbered Steam game reviews. Some are written in English and "
            "some are not. For EVERY review that is NOT written in English, return "
            "its index and a natural, concise English translation. Do NOT return "
            "anything for reviews that are already in English.\n\n"
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
        original = " ".join((ex.get("text", "") or "").split())[:300].strip()
        # Store only genuine translations (skip blanks and English echoed back).
        if english and english.lower() != original.lower():
            ex["translation"] = english
            ex["en"] = 0   # a translated quote is, by definition, not English


def _attach_translations(analysis: dict, client) -> None:
    """
    Add an English 'translation' to every non-English example quote.

    A first pass lets the model detect and translate foreign quotes (any script,
    ignoring the unreliable Steam tag). A second pass then force-translates any
    clearly non-Latin quote (CJK, Hangul, Cyrillic, etc.) the first pass skipped,
    so foreign reviews are never left untranslated.
    """
    examples = []
    for rec in analysis.get("negative", []) + analysis.get("positive", []):
        if rec.get("theme") in ("noise", "unclear"):
            continue
        examples.extend(rec.get("examples", []))
    if not examples:
        return

    BATCH = 20   # smaller batches: the model omits fewer entries per call
    # Batches touch disjoint example dicts, so they run concurrently (each call
    # only waits on the API); collapses the translation stall on large reports.
    def _run(batches, force=False):
        if not batches:
            return
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            list(pool.map(lambda b: _translate_batch(client, b, force), batches))

    _run([examples[s:s + BATCH] for s in range(0, len(examples), BATCH)])

    # Safety net: force-translate obvious non-Latin quotes the first pass missed.
    missed = [ex for ex in examples
              if not ex.get("translation") and _looks_non_latin(ex.get("text", ""))]
    _run([missed[s:s + BATCH] for s in range(0, len(missed), BATCH)], force=True)


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


TRAINING_SAMPLE_SIZE = int(os.environ.get("TRAINING_SAMPLE_SIZE", "60"))


def _collect_training_sample_async(client, reviews, context):
    """
    Keep the distillation flywheel alive in local-only mode.

    When LOCAL_CLASSIFY_ONLY is on, no review hits the LLM during analysis, so no
    fresh (text, label) samples get collected. To fix that without touching load
    time, we LLM-classify a small RANDOM sample in a background daemon thread AFTER
    the report is already built and returned, then log those rows for future
    retraining. Best-effort: any failure (or the instance spinning down) is fine.
    Returns the Thread (handy for tests); production ignores it.
    """
    if TRAINING_SAMPLE_SIZE <= 0 or not reviews:
        return None
    import random
    import threading

    def _work():
        try:
            import training_data
            # Work on COPIES so we never disturb the already-built report, and drop
            # the heavy embedding from each copy.
            sample = [dict(r) for r in random.sample(
                reviews, min(TRAINING_SAMPLE_SIZE, len(reviews)))]
            for r in sample:
                r.pop("_embedding", None)
            classify_all(client, sample, context=context)   # real LLM labels, in place
            training_data.log_samples(sample)
            print(f"[training] background-collected {len(sample)} LLM-labeled samples.")
        except Exception as err:
            print(f"[training] background sample failed ({err}).")

    th = threading.Thread(target=_work, daemon=True)
    th.start()
    return th


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
    _t = time.time()

    # Step 1: fetch ALL reviews (reuse the saved file unless refreshing).
    report(3, "Fetching reviews")
    if refresh or not os.path.exists(paths["reviews"]):
        print(f"Fetching up to {max_reviews} reviews (all sentiments)...")
        reviews = fetch_reviews_balanced(app_id, max_reviews=max_reviews)
        save_reviews(reviews, app_id, "all")
    else:
        print(f"Reusing fetched reviews from {paths['reviews']}")
        with open(paths["reviews"], encoding="utf-8") as f:
            reviews = json.load(f)
    print(f"[timing] fetch: {time.time() - _t:.1f}s ({len(reviews)} reviews)"); _t = time.time()

    # Step 2: classify (sentiment, category, is_constructive). Maps to 10%-55%.
    report(10, "Classifying reviews")
    # Game-context blurb so the classifier can spot sarcasm (e.g. "family
    # friendly" on a gore game) given what the game actually is.
    game_context = fetch_game_context(app_id)

    classified = hybrid_classify_all(
        client, reviews,
        on_progress=lambda f: report(10 + int(f * 45), "Classifying reviews"),
        context=game_context,
    )
    save_classified(classified, paths["reviews"])
    print(f"[timing] classify: {time.time() - _t:.1f}s"); _t = time.time()

    # Step 3: theme negative and positive sides separately. Maps to 55%-98%.
    analysis = analyze_both(
        client, classified,
        on_progress=lambda f, msg: report(55 + int(f * 43), msg),
    )
    print(f"[timing] theme: {time.time() - _t:.1f}s"); _t = time.time()

    # Passively collect (review text, LLM label) samples for future distillation
    # of a fast local classifier. Best-effort; never blocks the analysis.
    try:
        import training_data
        llm_labeled = [r for r in classified if r.get("_llm_labeled")]
        if llm_labeled:
            training_data.log_samples(llm_labeled)
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
    print(f"[timing] enrich (urls+translations+authors): {time.time() - _t:.1f}s")

    # Record the game's true total review count, for the review-growth refresh gate.
    analysis["steam_total_reviews"] = fetch_review_total(app_id) or analysis.get("total_reviews", 0)
    analysis["header_image"] = fetch_app_header(app_id)   # real banner URL for report + share card

    report(100, "Done")

    # Persist the analysis (Redis if configured, else a local file).
    store.save_analysis(app_id, analysis, max_age_days)

    # Local-only mode logged nothing above; collect a background LLM sample so
    # future retrainings still get fresh data (does not affect load time).
    if not any(r.get("_llm_labeled") for r in classified):
        _collect_training_sample_async(client, classified, game_context)

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
