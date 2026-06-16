"""
search.py

Turn a typed game name into Steam app suggestions, using Steam's public
storesearch endpoint (no API key required). Powers the web app's search bar.
"""

import requests


STORE_SEARCH_URL = "https://store.steampowered.com/api/storesearch/"
HEADERS = {"User-Agent": "SteamSifter/0.1 (review analytics project)"}
REQUEST_TIMEOUT = 10


def search_games(query: str, limit: int = 8) -> list:
    """
    Search Steam for games matching `query`.

    Args:
        query: The text the user typed.
        limit: Max suggestions to return.

    Returns:
        A list of {"appid", "name", "image"} dicts (empty on error/blank query).
    """
    query = (query or "").strip()
    if not query:
        return []

    params = {"term": query, "l": "english", "cc": "US"}
    try:
        resp = requests.get(STORE_SEARCH_URL, params=params,
                            headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        # Network hiccup or bad JSON: return nothing rather than crashing.
        return []

    results = []
    for item in data.get("items", [])[:limit]:
        # Only games have an integer app id we can analyze.
        if item.get("id"):
            results.append({
                "appid": item["id"],
                "name": item.get("name", "Unknown"),
                "image": item.get("tiny_image", ""),
            })
    return results


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "counter strike"
    print(f"Search results for '{q}':")
    for g in search_games(q):
        print(f"  {g['appid']:>8}  {g['name']}")
