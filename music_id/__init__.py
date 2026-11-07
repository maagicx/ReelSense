"""Music identification by audio fingerprint, behind a small provider interface.

v1 has one provider, AudD (music_id/audd.py). Another (ACRCloud) only needs a module with
the same four members and a line in get_provider(). Spotify links missing from a
provider's answer are filled in by music_id/spotify.py.
"""
import os
from dataclasses import asdict, dataclass
from typing import Optional, Protocol

from media import UserError

PROVIDER = (os.environ.get("MUSIC_ID_PROVIDER") or "audd").strip().lower()


@dataclass
class Track:
    artist: Optional[str]
    title: Optional[str]
    album: Optional[str] = None
    release_date: Optional[str] = None
    isrc: Optional[str] = None
    offset_seconds: Optional[float] = None  # where in the video the track was heard
    score: Optional[int] = None  # only providers that grade their matches (AudD enterprise)
    spotify_url: Optional[str] = None
    apple_music_url: Optional[str] = None
    song_link: Optional[str] = None
    artwork: Optional[str] = None  # album art for the card (Spotify or Apple CDN), not for the model
    spotify_searched: Optional[int] = None  # SPOTIFY_SEARCH_VERSION of the lookup that ran; internal

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in (None, "")}


class Provider(Protocol):
    name: str

    def configured(self) -> bool: ...

    def identify_clip(self, path: str) -> list[Track]:
        """Tracks in a short clip (usually 0 or 1). One billed request."""

    def identify_full(self, path: str) -> list[Track]:
        """Every track in a long file, each with offset_seconds."""

    def full_cost(self, duration_s: float) -> int:
        """Billed requests identify_full spends on duration_s of audio."""


def get_provider() -> Provider:
    if PROVIDER == "audd":
        from music_id import audd
        return audd
    raise UserError(f"Unknown MUSIC_ID_PROVIDER '{PROVIDER}'. Only 'audd' is available.")


def fill_spotify(tracks: list[Track]) -> None:
    """Spotify is the link the user listens on: look up any track that came back without one."""
    from music_id import spotify
    if not spotify.configured():
        return
    for t in tracks:
        if not t.spotify_url:
            found = spotify.find(t.isrc, t.artist, t.title)
            t.spotify_searched = SPOTIFY_SEARCH_VERSION
            if found:
                t.spotify_url, art = found
                t.artwork = t.artwork or art
        elif not t.artwork:
            t.artwork = spotify.art(t.spotify_url)


SPOTIFY_SEARCH_VERSION = 3  # 2: plain-query fallback; 3: near spellings of the artist


def fill_art(tracks: list[dict]) -> bool:
    """For cached result dicts: album art where there is a Spotify link but no art, and
    one more Spotify search for a track the first lookup missed (marked spotify_searched
    so it isn't searched on every hit). True if anything changed."""
    from music_id import spotify
    if not spotify.configured():
        return False
    changed = False
    for t in tracks:
        # Searched with an older lookup (True = 1): try once more with the current one.
        if not t.get("spotify_url") and (t.get("spotify_searched") or 0) < SPOTIFY_SEARCH_VERSION:
            found = spotify.find(t.get("isrc"), t.get("artist"), t.get("title"))
            t["spotify_searched"], changed = SPOTIFY_SEARCH_VERSION, True
            if found:
                t["spotify_url"], image = found
                if image and not t.get("artwork"):
                    t["artwork"] = image
        if t.get("spotify_url") and not t.get("artwork"):
            if image := spotify.art(t["spotify_url"]):
                t["artwork"], changed = image, True
    return changed


# Compilation and placeholder credits: a match "by" these says nothing about who performs it.
GENERIC_ARTISTS = {"v a", "va", "v.a", "various", "various artists", "varios artistas", "artistes divers",
                   "unknown", "unknown artist", "anonymous", "traditional", "n a", "na"}


def generic_artist(artist: Optional[str]) -> bool:
    from music_id.spotify import norm
    return not norm(artist) or norm(artist) in {norm(a) for a in GENERIC_ARTISTS}


def _overlap(a: Optional[str], b: Optional[str]) -> bool:
    from music_id.spotify import norm
    a, b = norm(a), norm(b)
    return bool(a and b) and (a in b or b in a)


def doubts(track: dict, credit: Optional[tuple[str, str]]) -> list[str]:
    """Mechanical reasons to doubt a fingerprint match. Whether a song suits the post is
    left to Claude, which reads post_credit."""
    reasons = []
    if generic_artist(track.get("artist")):
        reasons.append("generic_artist")
    if not any(track.get(k) for k in ("spotify_url", "apple_music_url", "song_link")):
        reasons.append("no_links")
    if credit and not (_overlap(track.get("artist"), credit[0]) or _overlap(track.get("title"), credit[1])):
        reasons.append("credit_disagrees")
    if track.get("released_after_video"):
        reasons.append("released_after_video")
    return reasons
