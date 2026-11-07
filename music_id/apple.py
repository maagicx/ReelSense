"""Apple Music track search through Apple's public iTunes Search API (no key, no account).
Only for find_on_apple_music, which Claude calls when the user asks for Apple Music:
Spotify stays the primary link everywhere else. Never raises."""
import logging
import os
from typing import Optional

import requests

from music_id import spotify

SEARCH_URL = "https://itunes.apple.com/search"
# The store the links point to; music.apple.com links open in other countries too.
COUNTRY = (os.environ.get("APPLE_MUSIC_COUNTRY") or "gb").strip().lower()

log = logging.getLogger("transcribe-mcp")


def _search(term: str, limit: int) -> list[dict]:
    resp = requests.get(SEARCH_URL, timeout=15, params={
        "term": term, "media": "music", "entity": "song", "limit": limit, "country": COUNTRY})
    if not resp.ok:
        log.warning(f"[music] Apple search HTTP {resp.status_code}")
        return []
    return [r for r in resp.json().get("results") or [] if r.get("kind") == "song"]


def _candidate(r: dict) -> dict:
    art = r.get("artworkUrl100") or ""
    out = {
        "artist": r.get("artistName"),
        "title": r.get("trackName"),
        "album": r.get("collectionName"),
        "release_date": (r.get("releaseDate") or "")[:10],
        "apple_music_url": (r.get("trackViewUrl") or "").replace("&uo=4", "").replace("?uo=4", ""),
        "artwork": art.replace("100x100bb", "200x200bb") if art.startswith("https://") else None,
    }
    return {k: v for k, v in out.items() if v}


def _as_spotify_item(r: dict) -> dict:
    """The shape spotify's artist/title checks read, so both searches match the same way."""
    return {"name": r.get("trackName"), "artists": [{"name": r.get("artistName")}]}


def search(query: Optional[str] = None, artist: Optional[str] = None, title: Optional[str] = None,
           limit: int = 5) -> list[dict]:
    """Apple Music candidates for a free-text query, or for an artist and title (then only
    tracks by that artist, or a near spelling of it with the same title). [] on any failure."""
    limit = max(1, min(int(limit or 5), 10))
    try:
        if artist and title:
            same, other = [], []
            for r in _search(f"{title} {artist}", 25):
                item = _as_spotify_item(r)
                titled = spotify._same_title(item, title)
                if spotify._same_artist(item, artist) or (spotify._near_artist(item, artist) and titled):
                    (same if titled else other).append(_candidate(r))
            # The artist's other songs only when none has the title asked for.
            return (same or other)[:limit]
        q = (query or " ".join(x for x in (title, artist) if x)).strip()
        return [_candidate(r) for r in _search(q, limit)] if q else []
    except (requests.RequestException, ValueError) as exc:
        log.warning(f"[music] Apple search failed: {exc.__class__.__name__}")
        return []
