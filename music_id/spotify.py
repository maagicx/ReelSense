"""Spotify links for tracks the fingerprint provider didn't link: by ISRC first, else by
artist and title. Client-credentials auth (no user sign-in). Optional: without
SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET it does nothing. Never raises."""
import logging
import os
import difflib
import re
import threading
import time
import unicodedata
from typing import Optional

import requests

CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip()
TOKEN_URL = "https://accounts.spotify.com/api/token"
SEARCH_URL = "https://api.spotify.com/v1/search"
# Tracks are licensed per country: with a market, search returns only versions playable
# there (relinked to a playable copy where Spotify has one). Empty = any market.
MARKET = (os.environ.get("SPOTIFY_MARKET", "GB")).strip().upper()

log = logging.getLogger("transcribe-mcp")
_lock = threading.Lock()
_token: dict = {"value": None, "expires": 0.0}


def configured() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET)


def _request(method: str, url: str, **kw) -> requests.Response:
    """One retry on a timeout, 429 (waiting Retry-After, at most 5 s) or 5xx: a full scan
    sends ~20 searches in a row, and one hiccup shouldn't cost a track its link."""
    for attempt in range(2):
        try:
            resp = requests.request(method, url, timeout=15, **kw)
        except requests.RequestException:
            if attempt:
                raise
            time.sleep(1)
            continue
        if attempt or (resp.status_code != 429 and resp.status_code < 500):
            return resp
        try:
            time.sleep(min(5, int(resp.headers.get("Retry-After") or 1)))
        except ValueError:
            time.sleep(1)
    return resp


def _access_token() -> Optional[str]:
    with _lock:
        if _token["value"] and _token["expires"] > time.time() + 60:
            return _token["value"]
        resp = _request("POST", TOKEN_URL, data={"grant_type": "client_credentials"},
                        auth=(CLIENT_ID, CLIENT_SECRET))
        if not resp.ok:
            log.warning(f"[music] Spotify token refused: HTTP {resp.status_code}")
            return None
        body = resp.json()
        _token.update(value=body["access_token"], expires=time.time() + int(body.get("expires_in", 3600)))
        return _token["value"]


def _search(q: str, token: str, limit: int = 5) -> list[dict]:
    params = {"q": q, "type": "track", "limit": limit, **({"market": MARKET} if MARKET else {})}
    resp = _request("GET", SEARCH_URL, params=params,
                    headers={"Authorization": f"Bearer {token}"})
    if not resp.ok:
        log.warning(f"[music] Spotify search HTTP {resp.status_code}")
        return []
    return (resp.json().get("tracks") or {}).get("items") or []


def norm(s: Optional[str]) -> str:
    """Lowercase, no accents, punctuation as spaces: "Rama Djoxs" == "RAMA DJOXS", "Beyoncé" == "Beyonce"."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^\w]+", " ", s).split())


def _same_artist(item: dict, artist: str) -> bool:
    wanted = norm(artist)
    return bool(wanted) and any(
        (name := norm(a.get("name"))) and (name in wanted or wanted in name)
        for a in item.get("artists") or [])


def _near_artist(item: dict, artist: str) -> bool:
    """A spelling variant of the artist ("Rama Djoxs" vs "Rama Djocks"): close but not equal."""
    wanted = norm(artist)
    return bool(wanted) and any(difflib.SequenceMatcher(None, wanted, norm(a.get("name"))).ratio() >= 0.8
                                for a in item.get("artists") or [] if a.get("name"))


def _same_title(item: dict, title: str) -> bool:
    """Same title, ignoring a trailing "(feat. …)", "- Remastered" and the like."""
    got, wanted = norm(item.get("name")), norm(title)
    return bool(wanted) and (got == wanted or got.startswith(wanted + " ") or wanted.startswith(got + " "))


def _match(items: list[dict], artist: str, title: str) -> Optional[dict]:
    """The artist must match, so a cover or namesake isn't returned; a near spelling of
    the artist counts only when the title matches too."""
    for item in items:
        if _same_artist(item, artist):
            return item
    for item in items:
        if _near_artist(item, artist) and _same_title(item, title):
            return item
    return None


def _candidate(item: dict) -> dict:
    from music_id.audd import album_art
    album = item.get("album") or {}
    out = {
        "artist": ", ".join(a["name"] for a in item.get("artists") or [] if a.get("name")),
        "title": item.get("name"),
        "album": album.get("name"),
        "release_date": album.get("release_date"),
        "isrc": (item.get("external_ids") or {}).get("isrc"),
        "spotify_url": (item.get("external_urls") or {}).get("spotify"),
        "artwork": album_art(album.get("images")),
    }
    return {k: v for k, v in out.items() if v}


def _found(item: dict) -> Optional[tuple[str, Optional[str]]]:
    from music_id.audd import album_art
    url = (item.get("external_urls") or {}).get("spotify")
    return (url, album_art((item.get("album") or {}).get("images"))) if url else None


def art(track_url: str) -> Optional[str]:
    """Album art of a Spotify track link (open.spotify.com/track/<id>), or None. Never raises."""
    track_id = track_url.rstrip("/").split("/")[-1].split("?")[0]
    if "/track/" not in track_url or not track_id.isalnum():
        return None
    try:
        token = _access_token()
        if not token:
            return None
        resp = _request("GET", f"https://api.spotify.com/v1/tracks/{track_id}",
                        headers={"Authorization": f"Bearer {token}"})
        if not resp.ok:
            log.warning(f"[music] Spotify track HTTP {resp.status_code}")
            return None
        _, image = _found(resp.json()) or (None, None)
        return image
    except (requests.RequestException, ValueError, KeyError) as exc:
        log.warning(f"[music] Spotify art lookup failed: {exc.__class__.__name__}")
        return None


def find(isrc: Optional[str], artist: Optional[str], title: Optional[str]) -> Optional[tuple[str, Optional[str]]]:
    """(track link, album art) or None."""
    try:
        token = _access_token()
        if not token:
            return None
        if isrc:
            items = _search(f"isrc:{isrc}", token)
            if items:
                return _found(items[0])
        if artist and title:
            # A text search can land on a cover or a namesake, so the artist must match.
            # Field filters miss some catalogue spellings, so a plain query is tried second.
            # The plain query also lists tracks by a near spelling of the artist's name.
            for q in (f"track:{title} artist:{artist}", f"{title} {artist}", title):
                if item := _match(_search(q, token, 10), artist, title):
                    return _found(item)
            log.info(f"[music] Spotify has no match for {artist!r} - {title!r}")
    except (requests.RequestException, ValueError, KeyError) as exc:
        log.warning(f"[music] Spotify lookup failed: {exc.__class__.__name__}")
    return None


def search(query: Optional[str] = None, artist: Optional[str] = None, title: Optional[str] = None,
           limit: int = 5) -> list[dict]:
    """Spotify track candidates for a free-text query, or for an artist and title (then
    only tracks by that artist). [] when nothing matches or Spotify isn't set up. Never raises."""
    limit = max(1, min(int(limit or 5), 10))
    try:
        token = _access_token() if configured() else None
        if not token:
            return []
        if artist and title:
            seen, out = set(), []
            for q in (f"track:{title} artist:{artist}", f"{title} {artist}", title):
                for item in _search(q, token, 10):
                    ok = _same_artist(item, artist) or (_near_artist(item, artist) and _same_title(item, title))
                    if ok and item.get("id") not in seen:
                        seen.add(item.get("id"))
                        out.append(_candidate(item))
                if out:
                    break
            return out[:limit]
        q = query or " ".join(x for x in (title, artist) if x)
        if not q.strip():
            return []
        return [_candidate(item) for item in _search(q.strip(), token, limit)]
    except (requests.RequestException, ValueError, KeyError) as exc:
        log.warning(f"[music] Spotify search failed: {exc.__class__.__name__}")
        return []
