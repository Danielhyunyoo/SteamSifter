"""
fetch_news.py

Fetches a game's recent updates / patch notes from Steam's public news API, so
the report can mark when updates happened on the sentiment-over-time chart. This
lets a viewer connect a dip or recovery in reviews to a specific patch date.

Endpoint (free, no API key required):
    https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/?appid=<id>
"""

import requests


NEWS_URL = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
HEADERS = {"User-Agent": "SteamSifter/0.1 (review analytics project)"}
REQUEST_TIMEOUT = 15


def fetch_patches(app_id, count: int = 30) -> list:
    """
    Return recent Steam update items for a game, newest first.

    Each item is {"date": unix_seconds, "title": str, "url": str,
    "patchnote": bool}. Returns an empty list on any failure, so a news outage
    never blocks an analysis.
    """
    # maxlength=1 keeps the payload tiny: we only need titles and dates.
    params = {"appid": app_id, "count": count, "maxlength": 1, "format": "json"}
    try:
        resp = requests.get(NEWS_URL, params=params, headers=HEADERS,
                            timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        items = resp.json().get("appnews", {}).get("newsitems", [])
    except (requests.RequestException, ValueError):
        return []

    patches = []
    for it in items:
        tags = it.get("tags") or []
        patches.append({
            "date": it.get("date", 0),
            "title": (it.get("title") or "").strip(),
            "url": it.get("url", ""),
            "patchnote": "patchnotes" in tags,
        })
    return patches


if __name__ == "__main__":
    import sys
    app = sys.argv[1] if len(sys.argv) > 1 else "730"
    for p in fetch_patches(app, count=10):
        from datetime import datetime, timezone
        d = datetime.fromtimestamp(p["date"], tz=timezone.utc).strftime("%Y-%m-%d")
        flag = "[patch]" if p["patchnote"] else "[news] "
        print(d, flag, p["title"][:70])
