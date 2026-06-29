"""
search.py

Turn a typed game name into Steam app suggestions.

Primary source is Steam's storefront search-suggest endpoint with the games
filter (f=games). It ranks actual games well and drops the DLC/soundtrack/demo
entries that the older storesearch API mixes in, so niche titles are not buried
under another game's add-ons (e.g. searching "diorama" surfaces the game itself,
not a dozen "Diorama Builder" packs). Falls back to the storesearch JSON API if
the suggest endpoint is unavailable or returns nothing, so search never regresses
to empty. Powers the web app's search bar.
"""

import re
from html import unescape

import requests


SUGGEST_URL = "https://store.steampowered.com/search/suggest"
STORE_SEARCH_URL = "https://store.steampowered.com/api/storesearch/"
HEADERS = {"User-Agent": "SteamSifter/0.1 (review analytics project)"}
REQUEST_TIMEOUT = 10

# Steam capsule image, built straight from the app id (the suggest HTML's own
# thumbnail uses this same asset path).
CAPSULE_URL = "https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/capsule_231x87.jpg"

# Each suggest row is an <a> linking to /app/<id>/, with the game name in a
# .match_name div. We key the app id off the href (very stable) and read the name.
_ANCHOR_RE = re.compile(r'<a\b[^>]*?href="[^"]*?/app/(\d+)[^"]*"[^>]*>(.*?)</a>', re.S | re.I)
_NAME_RE = re.compile(r'match_name[^>]*>([^<]+)<', re.I)
_PRICE_TAIL_RE = re.compile(r'\s*(?:[$£€][\d.,]+|Free(?:\s*To\s*Play)?)\s*$', re.I)
_TAG_RE = re.compile(r'<[^>]+>')
_IMG_RE = re.compile(r'<img[^>]+src="([^"]+)"', re.I)


def _parse_suggest(html: str, limit: int) -> list:
    """Pull {appid, name, image} rows out of the suggest endpoint's HTML."""
    results = []
    seen = set()
    for m in _ANCHOR_RE.finditer(html):
        appid = m.group(1)
        if appid in seen:
            continue
        inner = m.group(2)
        nm = _NAME_RE.search(inner)
        if nm:
            name = nm.group(1).strip()
        else:
            # No match_name div: strip tags, then drop a trailing price token.
            text = " ".join(_TAG_RE.sub(" ", inner).split())
            name = _PRICE_TAIL_RE.sub("", text).strip()
        name = unescape(name)
        if not name:
            continue
        seen.add(appid)
        img = _IMG_RE.search(inner)            # real capsule from the suggest HTML
        image = img.group(1) if img else CAPSULE_URL.format(appid=appid)
        results.append({"appid": int(appid), "name": name, "image": image})
        if len(results) >= limit:
            break
    return results


def _search_storesearch(query: str, limit: int) -> list:
    """Fallback: the older storesearch JSON API (games + some DLC mixed in)."""
    params = {"term": query, "l": "english", "cc": "US"}
    resp = requests.get(STORE_SEARCH_URL, params=params,
                        headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    out = []
    for item in data.get("items", [])[:limit]:
        if item.get("id"):
            out.append({"appid": item["id"], "name": item.get("name", "Unknown"),
                        "image": item.get("tiny_image", "")})
    return out


def search_games(query: str, limit: int = 15) -> list:
    """
    Search Steam for games matching `query`.

    Returns a list of {"appid", "name", "image"} dicts (empty on blank query or
    total failure). Tries the games-filtered suggest endpoint first for clean,
    well-ranked game results; falls back to the storesearch JSON API.
    """
    query = (query or "").strip()
    if not query:
        return []

    try:
        params = {"term": query, "f": "games", "cc": "US", "l": "english",
                  "use_store_query": 1, "use_search_spellcheck": 1}
        resp = requests.get(SUGGEST_URL, params=params,
                            headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        results = _parse_suggest(resp.text, limit)
        if results:
            return results
    except (requests.RequestException, ValueError):
        pass

    try:
        return _search_storesearch(query, limit)
    except (requests.RequestException, ValueError):
        return []


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "diorama"
    print(f"Search results for '{q}':")
    for g in search_games(q):
        print(f"  {g['appid']:>8}  {g['name']}")
