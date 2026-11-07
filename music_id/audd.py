"""AudD (https://docs.audd.io): the standard endpoint for short clips (1 request each) and
the enterprise endpoint for whole files (1 request per 12 s of audio scanned)."""
import logging
import math
import os
import time
from typing import Optional

import requests

from media import UserError, parse_time
from music_id import Track

name = "audd"
API_URL = "https://api.audd.io/"
ENTERPRISE_URL = "https://enterprise.audd.io/"
TOKEN = os.environ.get("AUDD_API_TOKEN", "").strip()
# Enterprise: 12 s chunks skipped after each one scanned. 2 = one chunk in three, a third
# of the cost, and any track playing for ~36 s or more is still caught.
FULL_SKIP = int(os.environ.get("MUSIC_FULL_SKIP") or 2)
CHUNK_S = 12
ATTEMPTS = 3

log = logging.getLogger("transcribe-mcp")

_ERRORS = {
    900: "AudD rejected the server's API token (AUDD_API_TOKEN).",
    901: "AudD rejected the request: no API token was sent and the free limit is used up.",
    400: "The audio is too large for AudD.",
    500: "AudD couldn't read the audio file.",
    700: "AudD received no audio.",
}


def configured() -> bool:
    return bool(TOKEN)


def full_cost(duration_s: float) -> int:
    chunks = math.ceil((duration_s or 0) / CHUNK_S)
    return max(1, math.ceil(chunks / (1 + FULL_SKIP)))


def _post(url: str, path: str, data: dict, timeout: int):
    """AudD's `result`, or None when it found no fingerprint to match."""
    resp = None
    for attempt in range(ATTEMPTS):
        try:
            with open(path, "rb") as f:
                # Token in the form body, never the URL, so it can't end up in a proxy log.
                resp = requests.post(url, data={"api_token": TOKEN, **data},
                                     files={"file": (os.path.basename(path), f, "audio/mpeg")},
                                     timeout=timeout)
        except requests.RequestException:
            resp = None
        if resp is not None and resp.status_code < 500 and resp.status_code != 429:
            break
        if attempt < ATTEMPTS - 1:
            time.sleep(2 ** (attempt + 1))
    if resp is None:
        raise UserError("Music recognition service (AudD) unreachable. Try again shortly.")
    if not resp.ok:
        raise UserError(f"Music recognition failed (AudD HTTP {resp.status_code}).")
    try:
        body = resp.json()
    except ValueError:
        raise UserError("Music recognition service (AudD) sent an unreadable answer.")
    if body.get("status") == "error":
        err = body.get("error") or {}
        code, msg = err.get("error_code"), str(err.get("error_message") or "")
        if code == 300:  # fingerprinting failed: clip too short or silent, so nothing to match
            return None
        log.warning(f"[music] AudD error {code}: {msg[:200]}")
        if code in _ERRORS:
            raise UserError(_ERRORS[code])
        if "limit" in msg.lower():
            raise UserError("The server's AudD request allowance is used up.")
        raise UserError(f"Music recognition failed (AudD error #{code}).")
    return body.get("result") or None


def identify_clip(path: str) -> list[Track]:
    r = _post(API_URL, path, {"return": "spotify,apple_music"}, timeout=60)
    if not isinstance(r, dict):
        return []
    sp, am = r.get("spotify") or {}, r.get("apple_music") or {}
    apple_art = (am.get("artwork") or {}).get("url") or ""
    return [Track(
        artwork=album_art((sp.get("album") or {}).get("images"))
        or (apple_art.replace("{w}", "160").replace("{h}", "160") if apple_art.startswith("https://") else None),
        artist=r.get("artist"),
        title=r.get("title"),
        album=r.get("album") or (sp.get("album") or {}).get("name") or am.get("albumName"),
        release_date=r.get("release_date") or (sp.get("album") or {}).get("release_date"),
        isrc=(sp.get("external_ids") or {}).get("isrc") or am.get("isrc"),
        spotify_url=(sp.get("external_urls") or {}).get("spotify"),
        apple_music_url=am.get("url"),
        song_link=r.get("song_link"),
    )]


def album_art(images) -> Optional[str]:
    """The smallest Spotify album image at least 128 px wide (they come largest first)."""
    usable = [i for i in images or [] if str(i.get("url") or "").startswith("https://")]
    big_enough = [i for i in usable if (i.get("width") or 0) >= 128]
    pick = min(big_enough, key=lambda i: i.get("width") or 0) if big_enough else (usable[0] if usable else None)
    return pick["url"] if pick else None


def _offset(value) -> Optional[float]:
    try:
        return parse_time(value)
    except UserError:
        return None


def identify_full(path: str) -> list[Track]:
    """One Track per run of chunks matching the same song, at the run's first offset.
    The enterprise endpoint returns no Spotify/Apple blocks; fill_spotify() adds links."""
    chunks = _post(ENTERPRISE_URL, path, {"skip": FULL_SKIP, "every": 1, "accurate_offsets": "true"},
                   timeout=900)
    tracks: list[Track] = []
    last = None
    for chunk in chunks if isinstance(chunks, list) else []:
        songs = [s for s in chunk.get("songs") or [] if s.get("title")]
        if not songs:
            last = None
            continue
        s = max(songs, key=lambda x: x.get("score") or 0)
        key = ((s.get("artist") or "").lower(), s["title"].lower())
        if key == last:
            tracks[-1].score = max(tracks[-1].score or 0, s.get("score") or 0) or None
            continue
        last = key
        start = _offset(chunk.get("offset"))
        if start is not None and s.get("start_offset"):
            start += s["start_offset"] / 1000
        tracks.append(Track(artist=s.get("artist"), title=s.get("title"), album=s.get("album"),
                            release_date=s.get("release_date"), isrc=s.get("isrc"),
                            offset_seconds=round(start) if start is not None else None,
                            score=s.get("score"), song_link=s.get("song_link")))
    return tracks
