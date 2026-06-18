"""
fetch_reviews.py

Pulls reviews for a single Steam game from the public Steam reviews API and
saves them to the data/ folder as clean JSON.

No AI is involved at this stage. The job here is purely: get real review data
out of Steam and into a tidy shape we can build on later.

Steam endpoint (free, no API key required):
    https://store.steampowered.com/appreviews/<appid>?json=1

Pagination works with a 'cursor': the first request uses cursor="*", and each
response hands back a new cursor to pass into the next request. We keep going
until Steam stops returning reviews or we hit our target count.
"""

import argparse
import json
import os
import time
from datetime import datetime, timezone

import requests


# ----------------------------------------------------------------------------
# Configuration / constants
# ----------------------------------------------------------------------------

# Base URL for the public Steam reviews endpoint. The {app_id} gets filled in.
STEAM_REVIEWS_URL = "https://store.steampowered.com/appreviews/{app_id}"

# Steam allows up to 100 reviews per page. We request the max to reduce the
# number of round-trips needed.
MAX_PER_PAGE = 100

# A polite pause (in seconds) between page requests so we don't hammer Steam.
DEFAULT_REQUEST_DELAY = 0.5

# How long to wait on any single request before giving up (seconds).
REQUEST_TIMEOUT = 20

# A simple User-Agent so our requests look like a normal client.
HEADERS = {"User-Agent": "SteamSifter/0.1 (review analytics project)"}


# ----------------------------------------------------------------------------
# Parsing: turn one raw Steam review into a clean, minimal dictionary
# ----------------------------------------------------------------------------

def parse_review(raw: dict) -> dict:
    """
    Convert a single raw Steam review object into the tidy shape we care about.

    We deliberately keep only the fields that matter for SteamSifter, and we
    convert playtime from minutes (how Steam stores it) into hours (how humans
    think about it).

    Args:
        raw: One element from the API's "reviews" list.

    Returns:
        A flat dictionary with the fields we will use downstream.
    """
    # The "author" sub-object holds the behavioral signals (playtime, etc.).
    author = raw.get("author", {})

    # Steam stores playtime in minutes; convert to hours and round for sanity.
    playtime_at_review_min = author.get("playtime_at_review", 0) or 0
    playtime_forever_min = author.get("playtime_forever", 0) or 0

    # Convert the creation timestamp (Unix seconds) into a readable date string.
    created_ts = raw.get("timestamp_created", 0) or 0
    created_date = (
        datetime.fromtimestamp(created_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        if created_ts
        else None
    )

    return {
        "recommendation_id": raw.get("recommendationid"),
        "steamid": author.get("steamid"),
        "text": (raw.get("review") or "").strip(),
        "voted_up": raw.get("voted_up", False),            # True = positive
        "helpful_votes": raw.get("votes_up", 0),           # community "helpful"
        "funny_votes": raw.get("votes_funny", 0),
        "weighted_vote_score": float(raw.get("weighted_vote_score") or 0.0),
        "playtime_at_review_hours": round(playtime_at_review_min / 60, 1),
        "playtime_forever_hours": round(playtime_forever_min / 60, 1),
        "num_games_owned": author.get("num_games_owned", 0),
        "num_reviews_by_author": author.get("num_reviews", 0),
        "language": raw.get("language"),
        "steam_purchase": raw.get("steam_purchase", False),
        "received_for_free": raw.get("received_for_free", False),
        "early_access": raw.get("written_during_early_access", False),
        "timestamp_created": created_ts,
        "created_date": created_date,
    }


# ----------------------------------------------------------------------------
# Fetching: page through the API until we have enough reviews
# ----------------------------------------------------------------------------

def fetch_reviews(
    app_id: str,
    max_reviews: int = 300,
    review_type: str = "all",
    language: str = "english",
    request_delay: float = DEFAULT_REQUEST_DELAY,
) -> list:
    """
    Fetch reviews for the given Steam app_id, paginating until we hit
    max_reviews or run out of reviews.

    Args:
        app_id:        Numeric Steam application ID as a string (e.g. "730").
        max_reviews:   Stop once we've collected at least this many reviews.
        review_type:   "all", "positive", or "negative".
        language:      Review language filter (e.g. "english", "all").
        request_delay: Seconds to wait between page requests (be polite).

    Returns:
        A list of cleaned review dictionaries (see parse_review).
    """
    url = STEAM_REVIEWS_URL.format(app_id=app_id)

    collected = []
    cursor = "*"          # "*" tells Steam to start from the first page
    seen_cursors = set()  # guard against an infinite loop if a cursor repeats

    # Use a session so the TCP connection is reused across pages (faster).
    session = requests.Session()
    session.headers.update(HEADERS)

    while len(collected) < max_reviews:
        # Query parameters for this page. requests handles URL-encoding the
        # cursor (which can contain characters like '=').
        params = {
            "json": 1,
            "filter": "recent",          # newest first; predictable ordering
            "language": language,
            "review_type": review_type,
            "purchase_type": "all",
            "num_per_page": MAX_PER_PAGE,
            "cursor": cursor,
        }

        # Make the request, with a timeout so we never hang forever.
        response = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        payload = response.json()

        # success == 1 means Steam accepted the query. Anything else: stop.
        if payload.get("success") != 1:
            print(f"Steam returned success != 1 for app {app_id}. Stopping.")
            break

        page_reviews = payload.get("reviews", [])

        # No reviews on this page means we've reached the end.
        if not page_reviews:
            break

        # Parse and keep this page's reviews.
        for raw in page_reviews:
            collected.append(parse_review(raw))

        # Advance the cursor for the next page. If Steam hands back a cursor
        # we've already used, we're looping; stop to be safe.
        cursor = payload.get("cursor", "")
        if not cursor or cursor in seen_cursors:
            break
        seen_cursors.add(cursor)

        # Be polite between requests.
        time.sleep(request_delay)

    # We may have slightly overshot max_reviews on the last page; trim to exact.
    return collected[:max_reviews]


# ----------------------------------------------------------------------------
# Total count: how many reviews the game has right now (for the refresh gate)
# ----------------------------------------------------------------------------

def fetch_review_total(app_id: str, language: str = "english") -> int:
    """
    Return the game's current total review count for our filter, or None on
    failure. Uses Steam's query_summary, which comes back on the first page, so
    num_per_page=0 makes this a tiny, single request.
    """
    url = STEAM_REVIEWS_URL.format(app_id=app_id)
    params = {
        "json": 1,
        "language": language,
        "review_type": "all",
        "purchase_type": "all",
        "num_per_page": 0,
    }
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("success") != 1:
            return None
        total = int(payload.get("query_summary", {}).get("total_reviews", 0))
        return total or None
    except (requests.RequestException, ValueError, TypeError):
        return None


# ----------------------------------------------------------------------------
# Reviewer profiles: avatar + username (so identical quotes read as distinct people)
# ----------------------------------------------------------------------------

PLAYER_SUMMARY_URL = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"


def fetch_player_summaries(steamids: list) -> dict:
    """
    Return {steamid: {"name": str, "avatar": url}} for the given Steam IDs.

    Uses the Steam Web API (GetPlayerSummaries), which needs a free STEAM_API_KEY.
    Returns {} if the key is missing or on any failure, so this is best-effort.
    Batched up to 100 ids per request.
    """
    key = os.environ.get("STEAM_API_KEY")
    ids = [s for s in dict.fromkeys(steamids) if s]   # de-dupe, drop blanks
    if not key or not ids:
        return {}

    out = {}
    for start in range(0, len(ids), 100):
        params = {"key": key, "steamids": ",".join(ids[start:start + 100])}
        try:
            resp = requests.get(PLAYER_SUMMARY_URL, params=params,
                                headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            players = resp.json().get("response", {}).get("players", [])
        except (requests.RequestException, ValueError):
            continue
        for pl in players:
            out[pl.get("steamid")] = {
                "name": pl.get("personaname", ""),
                "avatar": pl.get("avatarmedium") or pl.get("avatar", ""),
            }
    return out


# ----------------------------------------------------------------------------
# Saving: write the cleaned reviews to data/ as JSON
# ----------------------------------------------------------------------------

def save_reviews(reviews: list, app_id: str, review_type: str, out_dir: str = "data") -> str:
    """
    Save the cleaned reviews to a JSON file under out_dir.

    Args:
        reviews:     The list of cleaned review dictionaries.
        app_id:      The Steam app ID (used in the filename).
        review_type: "all" / "positive" / "negative" (used in the filename).
        out_dir:     Folder to write into (created if missing).

    Returns:
        The path of the file that was written.
    """
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"reviews_{app_id}_{review_type}.json")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(reviews, f, ensure_ascii=False, indent=2)

    return out_path


# ----------------------------------------------------------------------------
# Command-line entry point
# ----------------------------------------------------------------------------

def main():
    """Parse command-line arguments and run a fetch."""
    parser = argparse.ArgumentParser(
        description="Fetch Steam reviews for a single game and save them to data/."
    )
    parser.add_argument("app_id", help="Steam application ID, e.g. 730 for CS2")
    parser.add_argument(
        "--max", type=int, default=300,
        help="Maximum number of reviews to fetch (default: 300)",
    )
    parser.add_argument(
        "--type", default="all", choices=["all", "positive", "negative"],
        help="Which reviews to fetch (default: all)",
    )
    parser.add_argument(
        "--language", default="english",
        help="Review language filter (default: english)",
    )
    args = parser.parse_args()

    print(f"Fetching up to {args.max} '{args.type}' reviews for app {args.app_id}...")
    reviews = fetch_reviews(
        app_id=args.app_id,
        max_reviews=args.max,
        review_type=args.type,
        language=args.language,
    )

    out_path = save_reviews(reviews, args.app_id, args.type)

    # A short summary so you can sanity-check the result at a glance.
    positive = sum(1 for r in reviews if r["voted_up"])
    negative = len(reviews) - positive
    print(f"Saved {len(reviews)} reviews to {out_path}")
    print(f"  Positive: {positive}  |  Negative: {negative}")


if __name__ == "__main__":
    main()
