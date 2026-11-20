"""yt-dlp download + ffmpeg transcode + URL normalisation.

Everything here is synchronous and runs on the server's worker pool.
Failures surface as UserError with a short message fit for a tool result.
"""
import base64
import contextvars
import functools
import glob
import html
import ipaddress
import json
import logging
import math
import os
import re
import shutil
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from urllib.parse import parse_qs, quote_plus, urlencode, urljoin, urlparse

import requests
import yt_dlp

log = logging.getLogger("transcribe-mcp")

TMP = os.environ.get("TMP_DIR", "/tmp")
COOKIES_FILE = os.environ.get("COOKIES_FILE") or None
AUDIO_MAX_MB = float(os.environ.get("AUDIO_MAX_MB", "200"))
SLEEP_MIN = float(os.environ.get("YTDLP_SLEEP_MIN", "1"))
SLEEP_MAX = float(os.environ.get("YTDLP_SLEEP_MAX", "5"))
# Descriptions are kept up to DESCRIPTION_KEEP_CHARS (full=true and the card get that);
# what the model reads by default is cut to MAX_DESCRIPTION_CHARS.
DESCRIPTION_KEEP_CHARS = 4000
MAX_DESCRIPTION_CHARS = int(os.environ.get("MAX_DESCRIPTION_CHARS") or 500)
MAX_TAGS = 100
MAX_LIST_ITEMS = int(os.environ.get("MAX_LIST_ITEMS") or 100)
MAX_POPULAR_SCAN = int(os.environ.get("MAX_POPULAR_SCAN") or 1000)
# Ranking by likes where a listing has none (YouTube): likes are read video by video for
# the LIKES_LOOKUP_TOP most viewed (at most MAX_LIKES_LOOKUP), this many at a time.
LIKES_LOOKUP_TOP = int(os.environ.get("LIKES_LOOKUP_TOP") or 30)
MAX_LIKES_LOOKUP = int(os.environ.get("MAX_LIKES_LOOKUP") or 60)
LIKES_LOOKUP_WORKERS = int(os.environ.get("LIKES_LOOKUP_WORKERS") or 4)
MAX_KEYWORDS = 10  # per listed video: its tags, or the hashtags in its caption
MAX_FRAMES = int(os.environ.get("MAX_FRAMES") or 10)
FRAME_WIDTH = int(os.environ.get("FRAME_WIDTH") or 768)
FRAME_SOURCE_HEIGHT = 720  # read frames from a stream no taller than this
# Fallback when ffmpeg can't read the stream itself (TikTok's CDN wants yt-dlp's TLS
# impersonation): yt-dlp downloads the whole video, up to this size.
FRAME_DOWNLOAD_MAX_MB = float(os.environ.get("FRAME_DOWNLOAD_MAX_MB") or 50)
SLIDE_MAX_MB = float(os.environ.get("SLIDE_MAX_MB") or 15)  # per TikTok slideshow image
MAX_COMMENTS = int(os.environ.get("MAX_COMMENTS") or 200)

# TikTok's video servers refuse some signed links at random (HTTP 403, about half of
# them in Nov 2026); a fresh extraction gives a fresh link. Tries per download.
TIKTOK_LINK_ATTEMPTS = int(os.environ.get("TIKTOK_LINK_ATTEMPTS") or 3)
TIKTOK_RETRY_PAUSE_S = 2.0
# The post's sound file stands in for the video's audio only if its length is this close.
SOUND_LENGTH_TOLERANCE_S = 2.0

# TikTok's CDN wants a desktop UA and a TikTok referer. Do NOT send these anywhere
# else: a pinned UA contradicts the TLS fingerprint impersonating extractors present
# and gets their requests rejected.
_TIKTOK_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.tiktok.com/",
}


class UserError(Exception):
    """A failure whose message is safe and useful to return to the caller."""


# ---------------------------------------------------------------- progress

class Progress:
    """A job's finished steps and what it is doing now: returned to the user with the
    result, and sent to the client while it waits. Only that job's own steps, never the
    server log, and nothing meant for the admin (cookie advice, raw errors)."""
    def __init__(self):
        self.steps: list[str] = []
        self.now: Optional[str] = None

    def status(self) -> str:
        return self.now or (self.steps[-1] if self.steps else "starting")


# Set by the server in the job's worker thread; None outside a tracked job.
_progress: contextvars.ContextVar[Optional[Progress]] = contextvars.ContextVar("progress", default=None)


def track(progress: Progress) -> contextvars.Token:
    return _progress.set(progress)


def untrack(token: contextvars.Token) -> None:
    _progress.reset(token)


def doing(what: str) -> None:
    """What the job is doing now, for the client's progress line."""
    if p := _progress.get():
        p.now = what


def since(t0: float) -> str:
    """Seconds since t0, for a step: "1.3s"."""
    return f"{time.time() - t0:.1f}s"


def file_mb(path: str) -> str:
    """A file's size for a step; never fails the job."""
    try:
        return f"{os.path.getsize(path) / (1024 * 1024):.1f} MB"
    except OSError:
        return "? MB"


def step(tag: str, text: str, log_detail: str = "") -> None:
    """A finished step: to the log under tag (with log_detail, for the admin only) and to
    the tracked job's steps."""
    log.info(f"[{tag}] {text}{log_detail}")
    if p := _progress.get():
        p.steps.append(text)
        p.now = None


# ---------------------------------------------------------------- URLs

# abcnews.com/video/<id> matches no extractor; the abcnews.go.com embed form does.
_ABCNEWS_RE = re.compile(r"^https?://(?:www\.)?abcnews\.com/video/(\d+)", re.I)
# Al Jazeera's /video/<section>/<date>/<slug> matches no extractor; /program/... serves
# the same article and falls through to the page-scrape path that finds the player.
_ALJAZEERA_RE = re.compile(
    r"^(https?://(?:\w+\.)?aljazeera\.\w+)/video/([^/]+/\d{4}/\d{1,2}/\d{1,2}/[^/?#]+)", re.I
)


def normalise_url(url: str) -> str:
    url = (url or "").strip()
    if not re.match(r"^https?://", url, re.I):
        raise UserError("Not a URL. Pass a full http(s) link to the video.")
    if _is_tiktok_short(url):
        url = _resolve_tiktok_short(url)
    if "tiktok.com" in url and "is_from_webapp" not in url:
        url += ("&" if "?" in url else "?") + "is_from_webapp=1&sender_device=pc"
    m = _ABCNEWS_RE.match(url)
    if m:
        url = f"https://abcnews.go.com/video/embed?id={m.group(1)}"
    m = _ALJAZEERA_RE.match(url)
    if m:
        url = f"{m.group(1)}/program/{m.group(2)}"
    host = urlparse(url).hostname
    if not is_public_host(host):
        raise UserError(f"{host or 'That link'} isn't a public internet address. Pass a public link to the video.")
    return url


_TIKTOK_SHORT_HOSTS = ("vm.tiktok.com", "vt.tiktok.com")
TIKTOK_SHORT_MAX_HOPS = 5


def _is_tiktok_host(host: Optional[str]) -> bool:
    host = (host or "").lower()
    return host == "tiktok.com" or host.endswith(".tiktok.com")


def _is_tiktok_short(url: str) -> bool:
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    return host in _TIKTOK_SHORT_HOSTS or (host in ("tiktok.com", "www.tiktok.com")
                                           and parts.path.startswith("/t/"))


@functools.lru_cache(maxsize=256)
def _resolve_tiktok_short(url: str) -> str:
    """The post a TikTok short link (vm.tiktok.com/…, tiktok.com/t/…) points to, without
    TikTok's tracking query. Resolved here, not by yt-dlp: its short-link extractor fails
    on slideshows (/photo/ links), and this lets probe() recognise them. Every hop must
    stay on TikTok over https. Cached: one job resolves the same link several times."""
    current = url
    for _ in range(TIKTOK_SHORT_MAX_HOPS):
        try:
            resp = requests.get(current, headers=_TIKTOK_HEADERS, allow_redirects=False,
                                timeout=10, stream=True)
            resp.close()
        except requests.RequestException:
            raise UserError("Couldn't open that TikTok short link. Try again, or pass the full post link.")
        location = resp.headers.get("Location") if resp.is_redirect else None
        if not location:
            break
        current = urljoin(current, location)
        parts = urlparse(current)
        if parts.scheme != "https" or not _is_tiktok_host(parts.hostname):
            raise UserError("That TikTok short link leads off TikTok. Pass the full post link instead.")
        if not _is_tiktok_short(current):
            return f"https://{parts.hostname}{parts.path}"
    raise UserError("Couldn't open that TikTok short link. Try again, or pass the full post link.")


def is_public_host(host: Optional[str]) -> bool:
    """True if host resolves, and only to public internet addresses. Keeps links (and the
    images fetched for cards) off the server's own network: localhost, the LAN, the Docker
    host, Tailscale's 100.64/10. Checks the link as given; the redirects and media URLs
    yt-dlp follows after that aren't covered."""
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, ValueError):
        return False
    for *_, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0].split("%")[0])
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            return False
    return bool(infos)


_PLATFORMS = {
    "youtube.com": "youtube", "youtu.be": "youtube", "tiktok.com": "tiktok",
    "instagram.com": "instagram", "facebook.com": "facebook", "fb.watch": "facebook",
    "x.com": "x", "twitter.com": "x", "vimeo.com": "vimeo",
}


# A YouTube channel root with no tab: yt-dlp lists its tabs (Videos, Shorts, Live) as
# nested playlists, which _flat_entry drops, so it would list nothing.
_YT_CHANNEL_RE = re.compile(
    r"^(https?://(?:www\.|m\.)?youtube\.com/(?:@[^/?#]+|channel/[^/?#]+|c/[^/?#]+|user/[^/?#]+))/?(\?[^#]*)?$",
    re.I,
)


def youtube_channel_url(url: str) -> str:
    """Point a bare YouTube channel URL at its Videos tab; anything else is unchanged."""
    m = _YT_CHANNEL_RE.match(url)
    return f"{m.group(1)}/videos{m.group(2) or ''}" if m else url


def platform_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    for domain, name in _PLATFORMS.items():
        if host == domain or host.endswith("." + domain):
            return name
    return host.removeprefix("www.") or "unknown"


def _is_tiktok_photo(url: str) -> bool:
    return "tiktok.com" in url and "/photo/" in url


_TIKTOK_PHOTO_RE = re.compile(r"/photo/(\d+)")


def _tiktok_photo_as_video(url: str) -> str:
    """A slideshow (/photo/<id>) as /video/<id>: yt-dlp has no extractor for /photo/
    links, but reads the same post as a video link, with its sound as the "audio" format."""
    return _TIKTOK_PHOTO_RE.sub(r"/video/\1", url, count=1)


PHOTO_POST_MESSAGE = ("This is a TikTok photo (slideshow) post: it has a sound but no speech. "
                      "Use get_frames to see its slides, or identify_music for the song.")


# ---------------------------------------------------------------- yt-dlp

class _QuietLogger:
    """Swallow yt-dlp's console output; errors still arrive as exceptions. Notes the
    one warning that matters: yt-dlp saying the cookies it was given are invalid
    (YouTube rotated them), which the error itself doesn't mention."""
    def __init__(self):
        self.stale_cookies = False

    def debug(self, msg): pass
    def info(self, msg): pass
    def error(self, msg): pass

    def warning(self, msg):
        if "cookies are no longer valid" in str(msg).lower():
            self.stale_cookies = True


def _base_opts(url: str) -> dict:
    opts = {
        "quiet": True,
        "noprogress": True,
        "logger": _QuietLogger(),
        "geo_bypass": True,
        "noplaylist": True,
        # Deliberately low: aggressive retries can themselves trigger IP bans.
        "retries": 5,
        "fragment_retries": 3,
    }
    if "tiktok.com" in url:
        opts["http_headers"] = dict(_TIKTOK_HEADERS)
    return opts


def _cookie_copy(tag: str) -> Optional[str]:
    """yt-dlp rewrites the jar, and the mounted one is read-only and shared,
    so each run gets its own writable copy. Returns the copy's path."""
    if not (COOKIES_FILE and os.path.exists(COOKIES_FILE)):
        return None
    path = os.path.join(TMP, f"{tag}.cookies.txt")
    shutil.copyfile(COOKIES_FILE, path)
    return path


def _is_youtube_target(target: str) -> bool:
    return target.startswith("ytsearch") or platform_of(target) == "youtube"


def _site(target: str) -> str:
    """The platform's display name, for log lines and messages."""
    return "YouTube" if _is_youtube_target(target) else _SITE_NAMES.get(platform_of(target), platform_of(target))


def _cookies_last(target: str) -> bool:
    """Sites tried without cookies first: public videos there need none, and a stale
    export makes them refuse even those. Only YouTube: TikTok's video servers refuse
    downloads from the server without cookies (HTTP 403), so a first try without them
    would only add a failed request."""
    return _is_youtube_target(target)


def _refused_link(exc: Exception, target: str) -> bool:
    return platform_of(target) == "tiktok" and "http error 403" in str(exc).lower()


def _ytdl(target: str, opts: dict, tag: str, download: bool = False,
          setup: Optional[Callable[[yt_dlp.YoutubeDL], None]] = None) -> dict:
    """yt-dlp's extract_info with the cookie policy, failures as UserError.

    Cookies go with every request except YouTube's first try. YouTube rotates the
    cookies of an open browser session within minutes of an export, and stale ones
    make it refuse downloads (HTTP 403) even of public videos, which need no cookies.
    So YouTube goes without first and retries with cookies only if that fails
    (age-restricted, members-only, private, a bot check).

    A TikTok link refused with a 403 is extracted again (a fresh signed link), up to
    TIKTOK_LINK_ATTEMPTS tries. setup(ydl) runs on each new YoutubeDL before it starts."""
    cookies = _cookie_copy(tag)
    jars = [None, cookies] if cookies and _cookies_last(target) else [cookies]
    try:
        for i, jar in enumerate(jars):
            for attempt in range(1, TIKTOK_LINK_ATTEMPTS + 1):
                logger = _QuietLogger()
                run = {**opts, "logger": logger, **({"cookiefile": jar} if jar else {})}
                try:
                    with yt_dlp.YoutubeDL(run) as ydl:
                        if setup:
                            setup(ydl)
                        return ydl.extract_info(target, download=download)
                except Exception as exc:
                    if attempt < TIKTOK_LINK_ATTEMPTS and _refused_link(exc, target):
                        step(tag, "TikTok refused the download link, retrying with a fresh one",
                             f" ({str(exc)[:200]})")
                        time.sleep(TIKTOK_RETRY_PAUSE_S)
                        continue
                    if i == len(jars) - 1:
                        raise _failure(exc, target, bool(jar), logger.stale_cookies) from exc
                    step(tag, f"{_site(target)} without cookies failed, retrying with cookies",
                         f" ({str(exc)[:200]})")
                    break
    finally:
        if cookies:
            _rm(cookies)


def _date(yyyymmdd: Optional[str]) -> Optional[str]:
    if yyyymmdd and len(yyyymmdd) == 8:
        return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"
    return yyyymmdd


def _put(out: dict, key: str, value) -> None:
    """Set out[key] only when the platform actually supplied something."""
    if value not in (None, "", [], {}):
        out[key] = value


def _clean_list(values) -> list[str]:
    out, seen = [], set()
    for v in values or []:
        if isinstance(v, str) and (v := v.strip()) and v.lower() not in seen:
            seen.add(v.lower())
            out.append(v)
    return out[:MAX_TAGS]


def _spans(items, label: str) -> list[dict]:
    """chapters -> [{start, end, title}], heatmap -> [{start, end, value}]."""
    out = []
    for it in items or []:
        if not isinstance(it, dict) or it.get("start_time") is None or it.get(label) is None:
            continue
        span = {"start": round(it["start_time"], 2), "end": round(it.get("end_time") or it["start_time"], 2)}
        span[label] = round(it[label], 4) if label == "value" else it[label]
        out.append(span)
    return out


def _music(info: dict) -> dict:
    music = {}
    for key in ("track", "artist", "album"):
        _put(music, key, info.get(key) or (info.get(key + "s") or [None])[0])
    return music


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


# Query parameters that say which video a link is (YouTube's v and list, Facebook's id).
# Everything else (tracking tags, signed-link tokens, timestamps) stays out of the log.
_LOG_QUERY_KEEP = ("v", "list", "id")


def log_url(url: str, limit: int = 300) -> str:
    """The link as the log shows it: no login, query string or fragment beyond the
    parameters that identify the video."""
    try:
        parts = urlparse((url or "").strip())
        query = parse_qs(parts.query)
    except ValueError:
        return "(unreadable link)"
    host = parts.hostname or ""
    if parts.port:
        host += f":{parts.port}"
    kept = urlencode([(k, query[k][0]) for k in _LOG_QUERY_KEEP if k in query])
    out = f"{parts.scheme}://{host}{parts.path}" + (f"?{kept}" if kept else "")
    return clip(out, limit)


_URL_IN_TEXT_RE = re.compile(r"https?://[^\s'\"<>()]+")


def log_text(text: str, limit: int) -> str:
    """An error message for the log, with every link in it cut down by log_url."""
    return clip(_URL_IN_TEXT_RE.sub(lambda m: log_url(m.group(0), 200), str(text)), limit)


def _description(info: dict) -> str:
    return clip((info.get("description") or "").strip(), DESCRIPTION_KEEP_CHARS)


def _most_replayed(heatmap: list[dict], top: int = 3) -> list[dict]:
    """The heatmap's top spans as [{start, end}], in time order: what "most replayed"
    says in about 100 characters instead of the heatmap's ~100 points. Spans next to one
    already picked are skipped, so three peaks aren't one bump."""
    picked: list[dict] = []
    for span in sorted(heatmap, key=lambda h: h["value"], reverse=True):
        if all(span["start"] > p["end"] + 5 or span["end"] < p["start"] - 5 for p in picked):
            picked.append(span)
        if len(picked) == top:
            break
    # Outward to whole seconds: a short video's spans are a fraction of a second, and
    # rounding both ends would give "0 to 0".
    return [{"start": math.floor(p["start"]), "end": math.ceil(p["end"])}
            for p in sorted(picked, key=lambda p: p["start"])]


def _auto_captions(info: dict) -> bool:
    """Whether YouTube made speech-recognition captions. yt-dlp's automatic_captions also
    lists machine translations of other tracks (tlang=...), so only an untranslated
    kind=asr track counts."""
    for formats in (info.get("automatic_captions") or {}).values():
        for f in formats or []:
            query = parse_qs(urlparse(f.get("url") or "").query)
            if query.get("kind") == ["asr"] and "tlang" not in query:
                return True
    return False


def _summary(info: dict, url: str) -> dict:
    stats = {k: info.get(k) for k in ("view_count", "like_count", "comment_count")}
    out = {
        "title": info.get("title"),
        "channel": info.get("channel") or info.get("uploader") or info.get("creator"),
        "duration": info.get("duration"),
        "published": _date(info.get("upload_date")),
        "platform": platform_of(url),
        "url": info.get("webpage_url") or url,
        "stats": {k: v for k, v in stats.items() if v is not None},
    }
    # v2 enrichment: same extract_info call, and fields the platform didn't supply are omitted.
    _put(out, "description", _description(info))
    _put(out, "tags", _clean_list(info.get("tags")))
    _put(out, "categories", _clean_list(info.get("categories")))
    _put(out, "chapters", _spans(info.get("chapters"), "title"))
    _put(out, "thumbnail", info.get("thumbnail"))
    _put(out, "channel_url", info.get("channel_url") or info.get("uploader_url"))
    _put(out, "channel_id", info.get("channel_id"))
    _put(out, "channel_followers", info.get("channel_follower_count"))
    for key in ("live_status", "age_limit", "availability"):
        _put(out, key, info.get(key))
    _put(out, "heatmap", _spans(info.get("heatmap"), "value"))
    _put(out, "most_replayed", _most_replayed(out.get("heatmap") or []))
    _put(out, "music", _music(info))
    if out["platform"] == "youtube":
        out["auto_captions"] = _auto_captions(info)
    return out


def probe(url: str, tag: str, with_key: bool = False, allow_photo: bool = False) -> dict:
    """Metadata only, no download. Returns the summary dict; with_key adds video_key
    ("<extractor>:<id>", the same for every link to one video) for caching. A TikTok
    slideshow is refused unless allow_photo (it has a sound but no speech or video); then
    it is read as a video link and marked is_photo_post, with duration = the sound's."""
    url = normalise_url(url)
    photo = _is_tiktok_photo(url)
    if photo and not allow_photo:
        raise UserError(PHOTO_POST_MESSAGE)
    target = _tiktok_photo_as_video(url) if photo else url
    info = _ytdl(target, {**_base_opts(target), "skip_download": True}, f"{tag}-probe")
    if info.get("_type") == "playlist":
        raise UserError("That URL is a playlist or channel. Pass a single video link, "
                        "or use list_videos / channel_info.")
    out = _summary(info, url)
    if photo:
        parts = urlparse(url)
        out["url"] = f"https://{parts.hostname}{parts.path}"
        out["is_photo_post"] = True
    if with_key and info.get("id"):
        out["video_key"] = f"{info.get('extractor_key') or out['platform']}:{info['id']}"
    return out


_HASHTAG_RE = re.compile(r"#(\w[\w.]*)")


def _keywords(info: dict) -> list[str]:
    """A video's tags, or failing those the hashtags in its caption (TikTok has no tags)."""
    tags = _clean_list(info.get("tags"))
    if not tags:
        # The description, not the title: TikTok's title is the caption cut short, which
        # can slice a hashtag in two.
        text = info.get("description") or info.get("title") or ""
        tags = _clean_list(m.rstrip(".") for m in _HASHTAG_RE.findall(text))
    seen, out = set(), []
    for t in tags:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out[:MAX_KEYWORDS]


def _list_thumbnail(entry: dict) -> Optional[str]:
    """One thumbnail for a listed video: TikTok's still cover (its dynamicCover can be
    animated), else the smallest at least 320 px wide, else yt-dlp's best (the last)."""
    if isinstance(entry.get("thumbnail"), str):
        return entry["thumbnail"]
    thumbs = [t for t in entry.get("thumbnails") or [] if isinstance(t, dict) and t.get("url")]
    for name in ("cover", "originCover"):
        for t in thumbs:
            if t.get("id") == name:
                return t["url"]
    wide = [t for t in thumbs if (t.get("width") or 0) >= 320]
    if wide:
        return min(wide, key=lambda t: t["width"])["url"]
    return thumbs[-1]["url"] if thumbs else None


def _flat_entry(entry: dict) -> Optional[dict]:
    if entry.get("_type") == "playlist" or not entry.get("url"):
        return None  # a nested tab or sub-playlist, not a video
    published = _date(entry.get("upload_date")) or _ts_date(entry.get("timestamp"))
    out = {"url": entry.get("webpage_url") or entry["url"], "title": entry.get("title")}
    _put(out, "duration", entry.get("duration"))
    _put(out, "published", published)
    _put(out, "views", entry.get("view_count"))
    _put(out, "likes", entry.get("like_count"))
    _put(out, "comments", entry.get("comment_count"))
    _put(out, "shares", entry.get("repost_count"))
    _put(out, "keywords", _keywords(entry))
    _put(out, "thumbnail", _list_thumbnail(entry))
    return out


def _list_owner(info: dict) -> Optional[str]:
    """Whose videos a flat listing holds: the channel, else the first video's uploader."""
    first = next((e for e in info.get("entries") or [] if isinstance(e, dict)), {})
    return (info.get("channel") or info.get("uploader") or first.get("channel")
            or first.get("uploader") or info.get("title"))


def _look_up(videos: list[dict], tag: str) -> int:
    """Fills in likes (and comments, shares and keywords where missing) from one metadata
    read per video, for listings that carry none (YouTube's). LIKES_LOOKUP_WORKERS at a
    time; a video that can't be read keeps what it has. Returns how many got likes."""
    def one(item):
        i, v = item
        try:
            info = _ytdl(v["url"], {**_base_opts(v["url"]), "skip_download": True}, f"{tag}-lk{i}")
        except UserError:
            return False
        for key, src in (("likes", "like_count"), ("comments", "comment_count"),
                         ("shares", "repost_count"), ("views", "view_count")):
            if key not in v:
                _put(v, key, info.get(src))
        if "keywords" not in v:
            _put(v, "keywords", _keywords(info))
        # The video's own date is exact; YouTube's listing dates are "3 weeks ago" estimates.
        _put(v, "published", _date(info.get("upload_date")) or v.get("published"))
        return "likes" in v

    if not videos:
        return 0
    with ThreadPoolExecutor(max_workers=max(1, LIKES_LOOKUP_WORKERS)) as pool:
        return sum(pool.map(one, enumerate(videos)))


def _broken_extractor(url: str) -> Optional[str]:
    """IE_NAME of the extractor yt-dlp would use for url when yt-dlp itself marks it
    broken (_WORKING = False), e.g. instagram:user. Its failures look like login/bot
    checks, so without this the caller is wrongly told to refresh cookies."""
    for ie in yt_dlp.extractor.gen_extractor_classes():
        if ie.suitable(url):
            return None if ie.working() else ie.IE_NAME
    return None


def _flat_extract(target: str, tag: str, allow_video: bool = False, **extra) -> dict:
    """One flat (no per-video visits) yt-dlp pass over a channel/playlist/search target.
    allow_video returns a single video's info instead of refusing it."""
    if broken := _broken_extractor(target):
        platform = platform_of(target)
        raise UserError(f"yt-dlp can't currently read {platform} profiles or channels (its {broken} "
                        f"extractor is marked broken). Links to individual {platform} posts still work.")
    opts = {**_base_opts(target), "noplaylist": False, "skip_download": True,
            "extract_flat": "in_playlist", **extra}
    info = _ytdl(target, opts, tag)
    if info.get("_type") != "playlist" and not allow_video:
        raise UserError("That URL is a single video. Pass a channel, playlist or profile link.")
    return info


def list_flat(target: str, limit: int, tag: str) -> tuple[list[dict], bool, dict]:
    """Channel/playlist/profile URL or a yt-dlp search target ("ytsearchN:..."), listed
    without visiting each video. playlistend stops yt-dlp walking a huge channel.
    Returns (videos, dates_approximate, {channel?}): YouTube's flat listings carry no
    upload date, only "3 weeks ago" text, which approximate_date turns into a rough one."""
    limit = max(1, min(limit, MAX_LIST_ITEMS))
    is_search = target.startswith("ytsearch")
    if not is_search:
        target = youtube_channel_url(normalise_url(target))
    youtube = is_search or platform_of(target) == "youtube"
    extra = {"extractor_args": {"youtubetab": {"approximate_date": [""]}}} if youtube else {}
    info = _flat_extract(target, f"{tag}-list", playlistend=limit, **extra)
    entries = (_flat_entry(e) for e in info.get("entries") or [] if isinstance(e, dict))
    meta = {}
    if not is_search:
        _put(meta, "channel", _list_owner(info))
    return [e for e in entries if e][:limit], youtube, meta


RANKINGS = {"views": "views", "likes": "likes"}


def list_ranked(target: str, limit: int, by: str, tag: str) -> tuple[list[dict], bool, dict]:
    """Most-viewed or most-liked videos, ranked here: YouTube ignores sort=p in tab URLs and
    yt-dlp can't pick the Popular chip, so this reads up to MAX_POPULAR_SCAN newest entries
    per tab and sorts them. A bare YouTube channel covers its Videos and Shorts tabs.
    TikTok's listing has likes; YouTube's doesn't, so for by="likes" the LIKES_LOOKUP_TOP
    most viewed are read one by one and ranked by likes. Ranked rows without likes get
    them the same way. Returns (videos, dates_approximate, {scanned, channel?, ...notes})."""
    key = RANKINGS[by]
    limit = max(1, min(limit, MAX_LIST_ITEMS))
    url = normalise_url(target)
    root = _YT_CHANNEL_RE.match(url)
    tabs = [f"{root.group(1)}/videos", f"{root.group(1)}/shorts"] if root else [url]
    youtube = platform_of(url) == "youtube"
    extra = {"extractor_args": {"youtubetab": {"approximate_date": [""]}}} if youtube else {}
    videos, capped, owner = [], False, None
    for i, tab in enumerate(tabs):
        try:
            info = _flat_extract(tab, f"{tag}-pop{i}", playlistend=MAX_POPULAR_SCAN, **extra)
        except UserError:
            if i == 0:
                raise
            continue  # a channel without a Shorts tab
        owner = owner or _list_owner(info)
        entries = [e for e in (_flat_entry(x) for x in info.get("entries") or [] if isinstance(x, dict)) if e]
        capped |= len(entries) >= MAX_POPULAR_SCAN
        for e in entries:
            if "/shorts/" in e["url"]:
                e["short"] = True
        videos += entries
    meta = {"scanned": len(videos), "ranked_by": key}
    _put(meta, "channel", owner)
    if capped:
        meta["scan_note"] = f"Only the newest {MAX_POPULAR_SCAN} per tab were scanned; older videos aren't ranked."
    by_views = lambda v: v.get("views") or 0  # noqa: E731
    if key == "likes" and videos and not any("likes" in v for v in videos):
        videos.sort(key=by_views, reverse=True)
        videos = videos[:min(max(limit, LIKES_LOOKUP_TOP), MAX_LIKES_LOOKUP)]
        found = _look_up(videos, tag)
        meta["likes_note"] = (f"This platform's listing has no likes, so they were read for the "
                              f"{len(videos)} most viewed of {meta['scanned']} scanned ({found} had "
                              f"them); the ranking is among those.")
    elif not any(key in v for v in videos):
        meta["scan_note"] = f"This platform gave no {key} counts, so the order is the platform's own."
    videos.sort(key=lambda v: v.get(key) or 0, reverse=True)
    top = videos[:limit]
    if key == "views" and not any("likes" in v for v in videos):
        _look_up(top[:MAX_LIKES_LOOKUP], tag)  # likes for the rows shown
    return top, youtube, meta


# YouTube search "upload date" filter: name -> (protobuf enum in the sp= param, days).
# YouTube dropped sort-by-date (and yt-dlp its ytsearchdate: prefix), so "latest" is
# this filter plus a local sort. The filter is loose: once real matches run out YouTube
# pads with older videos, which the days cutoff removes.
UPLOAD_WINDOWS = {"hour": (1, 1), "day": (2, 1), "week": (3, 7), "month": (4, 31), "year": (5, 366)}


def search(query: str, limit: int, within: Optional[str], tag: str) -> tuple[list[dict], bool]:
    """YouTube keyword search; with `within`, only videos uploaded in that window, newest first."""
    limit = max(1, min(limit, MAX_LIST_ITEMS))
    if not within:
        return list_flat(f"ytsearch{limit}:{query}", limit, tag)[:2]
    code, days = UPLOAD_WINDOWS[within]
    # sp = {2: {1: upload date, 2: type video}}
    sp = base64.b64encode(bytes([0x12, 0x04, 0x08, code, 0x10, 0x01])).decode()
    url = f"https://www.youtube.com/results?search_query={quote_plus(query)}&sp={quote_plus(sp)}"
    # Only the top `limit` by relevance: deeper rows are weak fuzzy matches, which the
    # date sort below would otherwise push to the top. Fewer results beat wrong ones.
    videos, approx, _ = list_flat(url, limit, tag)
    # Dates are approximate ("2 weeks ago"), so allow a day of slack at the edge.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days + 1)).strftime("%Y-%m-%d")
    videos = [v for v in videos if v.get("published", "") >= cutoff]
    videos.sort(key=lambda v: v["published"], reverse=True)
    return videos[:limit], approx


def _avatar(thumbnails) -> Optional[str]:
    """YouTube mixes banners into a channel's thumbnails; its avatar is the square one."""
    thumbs = [t for t in thumbnails or [] if isinstance(t, dict) and t.get("url")]
    for t in thumbs:
        if t.get("id") == "avatar_uncropped":
            return t["url"]
    square = [t for t in thumbs if t.get("width") and t.get("width") == t.get("height")]
    return max(square, key=lambda t: t["width"])["url"] if square else None


def channel(url: str, tag: str) -> dict:
    """Channel/profile details from one flat pass. Only YouTube's tab page carries
    subscriber and verified fields, plus a video count from its uploads playlist.
    TikTok's extractor discards the profile stats, so followers, following, likes and
    videos come from the profile page; its channel fields sit on the entries rather than
    the playlist, hence one entry.
    A video link is resolved to the channel that posted it."""
    url = youtube_channel_url(normalise_url(url))
    info = _flat_extract(url, f"{tag}-channel", allow_video=True, playlistend=1)
    if info.get("_type") != "playlist":
        owner = info.get("uploader_url") or info.get("channel_url")
        if not owner:
            raise UserError("Couldn't find the channel that posted this video.")
        url = youtube_channel_url(owner)
        info = _flat_extract(url, f"{tag}-channel", playlistend=1)
    platform = platform_of(url)
    first = next((e for e in info.get("entries") or [] if isinstance(e, dict)), {})

    def pick(*keys):
        for src in (info, first):
            for k in keys:
                if src.get(k):
                    return src[k]
        return None

    # TikTok's uploader_id is a numeric user id; only an @name is a handle.
    handle = next((h for h in (pick("uploader_id"), info.get("webpage_url_basename"))
                   if str(h or "").startswith("@")), None)
    out = {"platform": platform}
    _put(out, "name", pick("channel", "uploader") or info.get("title"))
    _put(out, "handle", handle)
    _put(out, "channel_id", pick("channel_id"))
    _put(out, "url", pick("uploader_url", "channel_url") or info.get("webpage_url"))
    _put(out, "description", _description(info))
    _put(out, "subscribers", info.get("channel_follower_count"))
    _put(out, "verified", info.get("channel_is_verified"))
    page = {} if platform == "youtube" else profile_page(out.get("url") or url)
    _put(out, "avatar", _avatar(info.get("thumbnails")) or page.get("avatar"))
    out.update(page.get("stats") or {})
    if platform == "youtube":
        _put(out, "videos", _youtube_upload_count(out.get("channel_id"), tag))
    if not any(k in out for k in ("subscribers", "followers", "videos")):
        out["note"] = f"Channel statistics are not available for {platform} via yt-dlp."
    return out


def _youtube_upload_count(channel_id: Optional[str], tag: str) -> Optional[int]:
    """How many public uploads (videos, Shorts and past streams) a YouTube channel has:
    the count on its uploads playlist (UC... -> UU...), read without listing it."""
    if not (isinstance(channel_id, str) and channel_id.startswith("UC")):
        return None
    try:
        info = _flat_extract(f"https://www.youtube.com/playlist?list=UU{channel_id[2:]}",
                             f"{tag}-count", playlistend=1)
    except UserError:
        return None
    count = info.get("playlist_count")
    return count if isinstance(count, int) else None


# ---------------------------------------------------------------- card images

# Plain HTTP fetches for the channel/video card, not yt-dlp, so a pinned UA is fine here.
_BROWSER_HEADERS = {"User-Agent": _TIKTOK_HEADERS["User-Agent"]}
MAX_CARD_IMAGE_BYTES = 200_000
_TIKTOK_AVATAR_RE = re.compile(r'"avatarThumb"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"')
_OG_IMAGE_RE = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)'
    r'|<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', re.I)


# Platforms whose profile page (https://host/<name>) carries the avatar. Elsewhere, and on
# sub-pages like twitch.tv/<name>/videos, og:image is often just the site's logo.
_AVATAR_PAGES = {"tiktok", "twitch.tv", "soundcloud.com"}


_TIKTOK_STATS_RE = re.compile(r'"(statsV2|stats)"\s*:\s*(\{[^{}]*"followerCount"[^{}]*\})')
# TikTok profile stats -> channel_info's keys.
_TIKTOK_STAT_KEYS = {"followerCount": "followers", "followingCount": "following",
                     "heartCount": "likes", "videoCount": "videos"}


def _tiktok_stats(text: str) -> dict:
    """The profile's counts from its page JSON: statsV2 (exact, as strings) over stats
    (rounded). Only the user's own block has followerCount; each video's stats don't."""
    found = {}
    for m in _TIKTOK_STATS_RE.finditer(text):
        try:
            block = json.loads(m.group(2))
        except ValueError:
            continue
        found.setdefault(m.group(1), block)
    block = found.get("statsV2") or found.get("stats") or {}
    out = {}
    for src, key in _TIKTOK_STAT_KEYS.items():
        try:
            out[key] = int(block[src])
        except (KeyError, TypeError, ValueError):
            pass
    return out


def profile_page(url: str) -> dict:
    """What the profile page (https://host/<name>) gives, for platforms whose yt-dlp
    extractor drops it: {avatar?, stats?}. The avatar is TikTok's embedded page JSON
    (avatarThumb, ~4 KB), else og:image; stats are TikTok's followers, following, likes
    and videos. Best effort: {} on any failure. TikTok's avatar link is signed and expires."""
    parts = urlparse(url)
    name = parts.path.strip("/").split("/")[0]
    if platform_of(url) not in _AVATAR_PAGES or not name:
        return {}
    try:
        r = requests.get(f"https://{parts.hostname}/{name}", headers=_BROWSER_HEADERS, timeout=10)
        r.raise_for_status()
        text = r.text
    except requests.RequestException:
        return {}
    out: dict = {}
    try:
        if m := _TIKTOK_AVATAR_RE.search(text):
            found = json.loads(f'"{m.group(1)}"')
        elif m := _OG_IMAGE_RE.search(text):
            found = html.unescape(m.group(1) or m.group(2))
        else:
            found = ""
    except ValueError:
        found = ""
    if found.startswith("https://"):
        out["avatar"] = found
    if platform_of(url) == "tiktok":
        _put(out, "stats", _tiktok_stats(text))
    return out


def page_avatar(url: str) -> Optional[str]:
    return profile_page(url).get("avatar")


def _fetch_image(url: str, max_bytes: int) -> Optional[tuple[str, bytes]]:
    """(content type, bytes) of a public https image of at most max_bytes, else None."""
    try:
        for _ in range(5):  # redirects followed by hand, so each hop gets the same check
            if not url.startswith("https://") or not is_public_host(urlparse(url).hostname):
                return None
            with requests.get(url, headers=_BROWSER_HEADERS, timeout=10, stream=True,
                              allow_redirects=False) as r:
                if r.is_redirect:
                    url = urljoin(url, r.headers.get("location", ""))
                    continue
                r.raise_for_status()
                kind = r.headers.get("content-type", "").split(";")[0].strip().lower()
                if not kind.startswith("image/"):
                    return None
                body = r.raw.read(max_bytes + 1, decode_content=True)
                break
        else:
            return None
    except (requests.RequestException, OSError):
        return None
    if not body or len(body) > max_bytes:
        return None
    return kind, body


def image_data_uri(url: str) -> Optional[str]:
    """An image as a data: URI, for the card's _meta when the card can't load the URL
    itself (signed links that expire, CDNs that refuse other sites). None if it fails,
    isn't an image or is over MAX_CARD_IMAGE_BYTES."""
    got = _fetch_image(url, MAX_CARD_IMAGE_BYTES)
    if not got:
        return None
    kind, body = got
    return f"data:{kind};base64,{base64.b64encode(body).decode()}"


LIST_THUMB_WIDTH = 160  # px; a list row's thumbnail is shown at about half that
LIST_THUMB_SOURCE_BYTES = 3_000_000
LIST_THUMB_KEEP_BYTES = 15_000  # smaller than this is sent as it is


def small_image_data_uri(url: str, width: int = LIST_THUMB_WIDTH) -> Optional[str]:
    """A thumbnail for a list row as a data: URI, shrunk with ffmpeg to `width` px wide:
    TikTok's covers are 50-125 KB at full size, a list holds dozens of them, and its
    signed links expire. None on any failure."""
    got = _fetch_image(url, LIST_THUMB_SOURCE_BYTES)
    if not got:
        return None
    kind, body = got
    if len(body) <= LIST_THUMB_KEEP_BYTES:
        return f"data:{kind};base64,{base64.b64encode(body).decode()}"
    try:
        run = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", "pipe:0", "-frames:v", "1",
             "-vf", f"scale={width}:-2:flags=lanczos,format=yuvj420p",
             "-q:v", "5", "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1"],
            input=body, capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if run.returncode or not run.stdout or len(run.stdout) > MAX_CARD_IMAGE_BYTES:
        return None
    return f"data:image/jpeg;base64,{base64.b64encode(run.stdout).decode()}"


def _ts_date(ts) -> Optional[str]:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d") if ts else None


def comments(url: str, limit: int, tag: str) -> list[dict]:
    """Top-level comments, top-sorted. YouTube only: yt-dlp's TikTok extractor has no
    comment support, and YouTube is the only one where max_comments stops the walk."""
    url = normalise_url(url)
    if platform_of(url) != "youtube":
        raise UserError(f"Comments are only supported on YouTube, not {platform_of(url)}.")
    limit = max(1, min(limit, MAX_COMMENTS))
    opts = {
        **_base_opts(url),
        "skip_download": True,
        "getcomments": True,
        # max-comments, max-parents, max-replies, max-replies-per-thread: no replies.
        "extractor_args": {"youtube": {"max_comments": [str(limit), "all", "0", "0"], "comment_sort": ["top"]}},
    }
    info = _ytdl(url, opts, f"{tag}-comments")
    out = []
    for c in (info.get("comments") or [])[:limit]:
        item = {"author": c.get("author"), "text": c.get("text") or ""}
        _put(item, "likes", c.get("like_count"))
        _put(item, "published", _ts_date(c.get("timestamp")))
        if c.get("is_pinned"):
            item["is_pinned"] = True
        if c.get("author_is_uploader"):
            item["is_uploader"] = True
        out.append(item)
    return out


# ---------------------------------------------------------------- sections

def parse_time(value) -> Optional[float]:
    """Seconds from 90, "90", "1:30" or "1:02:30". None for None or ""."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
    else:
        parts = str(value).strip().split(":")
        try:
            if len(parts) > 3 or not all(p.strip() for p in parts):
                raise ValueError
            seconds = 0.0
            for p in parts:
                seconds = seconds * 60 + float(p)
        except ValueError:
            raise UserError(f"Can't read the time {value!r}. Use seconds or h:mm:ss, e.g. 90 or 1:02:30.")
    if seconds < 0:
        raise UserError("Times can't be negative.")
    return seconds


def _clock(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def resolve_section(meta: dict, start: Optional[float], end: Optional[float],
                    chapter: Optional[str]) -> Optional[dict]:
    """{start, end, chapter?} for the part to transcribe, or None for the whole video.
    chapter matches a title exactly (ignoring case), else as a unique substring."""
    duration = meta.get("duration")
    if chapter:
        chapters = meta.get("chapters") or []
        if not chapters:
            raise UserError("This video has no chapters. Pass start and end instead.")
        want = chapter.strip().lower()
        hits = [c for c in chapters if c["title"].strip().lower() == want] or \
               [c for c in chapters if want in c["title"].lower()]
        if len(hits) != 1:
            titles = "; ".join(c["title"] for c in chapters[:20])
            raise UserError(f"{'No' if not hits else 'More than one'} chapter matches "
                            f"{chapter!r}. Chapters: {titles}")
        start, end, chapter = hits[0]["start"], hits[0]["end"], hits[0]["title"]
    if start is None and end is None:
        return None
    start = start or 0.0
    if duration and start >= duration:
        raise UserError(f"Start {_clock(start)} is past the end of the video ({_clock(duration)}).")
    if end is None or (duration and end > duration):
        end = duration
    if end is not None and end <= start:
        raise UserError("End must be after start.")
    section = {"start": round(start, 2), "end": round(end, 2) if end is not None else None}
    if chapter:
        section["chapter"] = chapter
    return section


def _own_sound(info: dict) -> bool:
    """Whether a TikTok post's sound is the creator's own: its artist is the post's
    channel or handle. Then it is the video's own audio (the same length)."""
    names = {(info.get(k) or "").strip().casefold() for k in ("uploader", "channel", "creator")} - {""}
    artists = info.get("artists") or [info.get("artist")]
    return any(isinstance(a, str) and a.strip().casefold() in names for a in artists)


class _TikTokSound(yt_dlp.postprocessor.PostProcessor):
    """pre_process (before format selection): keeps TikTok's "audio" format only when
    allowed and it is the creator's own sound, else removes it. "audio" is the post's
    sound from TikTok's music servers: an mp3 that downloads first time, where the video
    servers refuse about half the links, and it's fetched without the pause before
    downloads. With a licensed track attached it is the whole
    song (187 s of Mozart for a 50 s video with a voiceover), not the video's audio."""
    def __init__(self, allow: bool, photo: bool = False):
        super().__init__()
        # photo: a slideshow, whose sound is all there is: always use it.
        self.allow, self.photo, self.used, self.duration = allow, photo, False, None

    def run(self, info):
        formats = info.get("formats") or []
        self.duration = info.get("duration")
        has_sound = any(f.get("format_id") == "audio" for f in formats)
        self.used = has_sound and (self.photo or (self.allow and bool(self.duration) and _own_sound(info)))
        if not self.used:
            info["formats"] = [f for f in formats if f.get("format_id") != "audio"]
        elif self._downloader:
            # One small file from the music servers: skip the 1-5 s pause before the
            # download, which is there to pace video downloads. yt-dlp reads it when the
            # download starts, after this hook.
            self._downloader.params.update(sleep_interval=0, max_sleep_interval=0)
        return [], info


def _audio_length(path: str) -> Optional[float]:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "csv=p=0", path], capture_output=True, text=True, timeout=30)
        return float(out.stdout.strip())
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return None


def download_audio(url: str, job_id: str, section: Optional[dict] = None, music: bool = False) -> str:
    """Download best audio and transcode to 16 kHz mono 64 kbps MP3 (speech), or with
    music=True 44.1 kHz mono 128 kbps (what fingerprinting wants). Returns its path.
    section {start, end} downloads only that range (yt-dlp seeks; the rest isn't fetched).

    On TikTok, a post with the creator's own sound downloads that sound file (quick and
    reliable); its length is then checked against the video's, and on a mismatch the
    video is downloaded instead. Other posts download the video."""
    url = normalise_url(url)
    tiktok = platform_of(url) == "tiktok"
    if tiktok and _is_tiktok_photo(url):
        # A slideshow: its sound file is the only audio, so no length check or fallback.
        path = _download_audio(_tiktok_photo_as_video(url), job_id, section, music,
                               _TikTokSound(allow=True, photo=True))
        step(job_id, "used the slideshow's sound file")
        return path
    sound = _TikTokSound(allow=True)
    path = _download_audio(url, job_id, section, music, sound if tiktok else None)
    if tiktok and sound.used:
        expected = ((section["end"] or sound.duration) - section["start"]) if section else sound.duration
        got = _audio_length(path)
        if got is None or abs(got - expected) > SOUND_LENGTH_TOLERANCE_S:
            step(job_id, "TikTok's sound file isn't the video's length, downloading the video instead",
                 f" (sound {got}s, video {expected}s)")
            _rm(path)
            path = _download_audio(url, job_id, section, music, _TikTokSound(allow=False))
        else:
            step(job_id, "used the post's own sound file (quicker than the video)")
    return path


def _download_audio(url: str, job_id: str, section: Optional[dict], music: bool,
                    sound: Optional[_TikTokSound]) -> str:
    opts = {
        **_base_opts(url),
        # On TikTok, "audio" survives only if _TikTokSound keeps it.
        "format": "audio/bestaudio/best" if sound else "bestaudio/best",
        # TikTok's HEVC formats are video-only despite advertising aac; prefer h264,
        # which actually carries audio, when no audio-only format exists.
        "format_sort": ["vcodec:h264"],
        "outtmpl": os.path.join(TMP, f"{job_id}.%(ext)s"),
        "sleep_interval": SLEEP_MIN,
        "max_sleep_interval": max(SLEEP_MIN, SLEEP_MAX),
        "sleep_interval_requests": 1,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
             "preferredquality": "128" if music else "64"}
        ],
        "postprocessor_args": ["-ac", "1", "-ar", "44100" if music else "16000"],
    }
    if section:
        end = section["end"] if section["end"] is not None else float("inf")
        opts["download_ranges"] = yt_dlp.utils.download_range_func(None, [(section["start"], end)])
        # Without this the cut lands on the stream's previous keyframe, up to ~10 s early,
        # and the segment timestamps (shifted by start) would be off by that much.
        opts["force_keyframes_at_cuts"] = True
    setup = (lambda ydl: ydl.add_post_processor(sound, when="pre_process")) if sound else None
    _ytdl(url, opts, job_id, download=True, setup=setup)

    path = os.path.join(TMP, f"{job_id}.mp3")
    if not os.path.exists(path):
        raise UserError("No audio track found in this video.")
    size_mb = os.path.getsize(path) / (1024 * 1024)
    if size_mb > AUDIO_MAX_MB:
        raise UserError(f"Audio is {size_mb:.0f} MB, over the {AUDIO_MAX_MB:.0f} MB limit.")
    return path


def cut_clip(src: str, start: float, length: float, out: str) -> Optional[str]:
    """length seconds of src from start, as 44.1 kHz mono 128 kbps MP3 at out. None if
    ffmpeg produced nothing (start past the end of the audio)."""
    cmd = ["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.2f}", "-t", f"{length:.2f}", "-i", src,
           "-ac", "1", "-ar", "44100", "-b:a", "128k", out]
    try:
        subprocess.run(cmd, capture_output=True, timeout=60)
    except subprocess.TimeoutExpired:
        return None
    return out if os.path.exists(out) and os.path.getsize(out) > 4096 else None


# ---------------------------------------------------------------- frames

def _video_stream(url: str, tag: str) -> tuple[dict, dict]:
    """(info, the chosen format) for a video stream of at most FRAME_SOURCE_HEIGHT,
    resolved without downloading. HLS first: YouTube's DASH links 403 plain ffmpeg, its
    HLS ones don't, and a seek there fetches only the few-second segment needed. h264
    first: every ffmpeg decodes it."""
    opts = {**_base_opts(url), "skip_download": True, "format": "bv*[protocol^=m3u8]/bv*/b",
            "format_sort": [f"res:{FRAME_SOURCE_HEIGHT}", "vcodec:h264"]}
    if _is_tiktok_photo(url):
        raise UserError("This is a TikTok photo (slideshow) post: it has no video. "
                        "Use get_frames without timestamps to see its slides.")
    info = _ytdl(url, opts, f"{tag}-frames")
    if info.get("_type") == "playlist":
        raise UserError("That URL is a playlist or channel. Pass a single video link.")
    fmt = next((f for f in info.get("requested_formats") or [] if f.get("vcodec") not in (None, "none")), info)
    if not fmt.get("url") or fmt.get("vcodec") == "none":
        raise UserError("This post has no video stream to take frames from.")
    return info, fmt


def _grab(source: str, t: float, header_arg: str = "") -> Optional[bytes]:
    """One JPEG at t seconds from a URL or file, or None if ffmpeg couldn't read it."""
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{t:.3f}"]
    if header_arg:
        cmd += ["-headers", header_arg]
    # Longest side at most FRAME_WIDTH, never upscaled: portrait video would otherwise
    # come back 720x1280 and cost Claude twice the tokens.
    fit = (f"scale='min({FRAME_WIDTH},iw)':'min({FRAME_WIDTH},ih)'"
           ":force_original_aspect_ratio=decrease:force_divisible_by=2")
    cmd += ["-i", source, "-frames:v", "1", "-vf", fit,
            "-q:v", "4", "-f", "image2", "-c:v", "mjpeg", "pipe:1"]
    try:
        run = subprocess.run(cmd, capture_output=True, timeout=60)
    except subprocess.TimeoutExpired:
        return None
    return run.stdout if run.returncode == 0 and run.stdout else None


def _download_video(url: str, tag: str) -> str:
    """The whole ≤FRAME_SOURCE_HEIGHT video via yt-dlp's own downloader, capped at
    FRAME_DOWNLOAD_MAX_MB. Removed by cleanup(tag)."""
    opts = {**_base_opts(url), "format": "bv*/b",
            "format_sort": [f"res:{FRAME_SOURCE_HEIGHT}", "vcodec:h264"],
            "max_filesize": int(FRAME_DOWNLOAD_MAX_MB * 1024 * 1024),
            "outtmpl": os.path.join(TMP, f"{tag}.%(ext)s")}
    _ytdl(url, opts, tag, download=True)
    files = [f for f in glob.glob(os.path.join(TMP, f"{glob.escape(tag)}.*"))
             if not f.endswith((".part", ".txt", ".ytdl"))]
    if not files:
        raise UserError(f"The video is over {FRAME_DOWNLOAD_MAX_MB:.0f} MB, too big to take frames "
                        "from on this platform.")
    return files[0]


def frames(url: str, times: list[float], tag: str) -> tuple[dict, list[tuple[float, bytes]]]:
    """(video summary, [(time, jpeg)]): one still per time, FRAME_WIDTH wide. ffmpeg
    seeks into the stream yt-dlp resolved, so only a little around each time is fetched.
    Where the platform refuses ffmpeg (TikTok), yt-dlp downloads the video, capped at
    FRAME_DOWNLOAD_MAX_MB. Times past the end are refused. Call cleanup(tag) after."""
    url = normalise_url(url)
    info, fmt = _video_stream(url, tag)
    duration = info.get("duration")
    late = [t for t in times if duration and t >= duration]
    if late:
        raise UserError(f"{_clock(late[0])} is past the end of the video ({_clock(duration)}).")
    headers = dict(fmt.get("http_headers") or {})
    if fmt.get("cookies"):
        headers["Cookie"] = fmt["cookies"]
    http = str(fmt.get("protocol") or "https").startswith("http")
    header_arg = "".join(f"{k}: {v}\r\n" for k, v in headers.items()) if http else ""
    source, out = fmt["url"], []
    for t in times:
        jpg = _grab(source, t, header_arg if source == fmt["url"] else "")
        if jpg is None and source == fmt["url"]:
            source = _download_video(url, tag)
            jpg = _grab(source, t)
        if jpg is None:
            raise UserError(f"Couldn't read the frame at {_clock(t)}.")
        out.append((t, jpg))
    return _summary(info, url), out


# ---------------------------------------------------------------- TikTok slideshows

# The post's data as TikTok's web page embeds it. yt-dlp reads the same blob but keeps
# only the cover image, so the slides come from here.
_REHYDRATION_RE = re.compile(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', re.S)
_TIKTOK_IMAGE_HOST_RE = re.compile(r"(?:^|\.)tiktokcdn(?:-[a-z]+)?\.com$")
_NO_SLIDES = ("TikTok didn't return this slideshow's slides (it may be asking for a sign-in). "
              "Try again later.")


def _slide_urls(html: str) -> list[list[str]]:
    """Each slide's image links (signed mirrors of one image), from the page's data blob."""
    m = _REHYDRATION_RE.search(html or "")
    try:
        data = json.loads(m.group(1)) if m else {}
    except ValueError:
        data = {}
    detail = ((data.get("__DEFAULT_SCOPE__") or {}).get("webapp.video-detail") or {}) if isinstance(data, dict) else {}
    item = (detail.get("itemInfo") or {}).get("itemStruct") or {}
    out = []
    for image in (item.get("imagePost") or {}).get("images") or []:
        urls = [u for u in ((image or {}).get("imageURL") or {}).get("urlList") or [] if isinstance(u, str)]
        if urls:
            out.append(urls)
    if not out:
        raise UserError(_NO_SLIDES)
    return out


def _slide_host_ok(url: str) -> bool:
    parts = urlparse(url)
    return (parts.scheme == "https" and bool(_TIKTOK_IMAGE_HOST_RE.search(parts.hostname or ""))
            and is_public_host(parts.hostname))


def _fetch_slide(urls: list[str], path: str) -> Optional[str]:
    """The first of a slide's mirror links that downloads (some refuse), to path, capped
    at SLIDE_MAX_MB; returns that link, or None. Only TikTok's image servers, and no
    redirects away from them."""
    cap = int(SLIDE_MAX_MB * 1024 * 1024)
    for url in urls:
        if not _slide_host_ok(url):
            continue
        try:
            with requests.get(url, headers=_TIKTOK_HEADERS, timeout=10, stream=True,
                              allow_redirects=False) as resp:
                if resp.status_code != 200:
                    continue
                size, chunks = 0, []
                for chunk in resp.iter_content(64 * 1024):
                    size += len(chunk)
                    if size > cap:
                        raise UserError(f"A slide is over the {SLIDE_MAX_MB:.0f} MB limit.")
                    chunks.append(chunk)
        except requests.RequestException:
            continue
        with open(path, "wb") as f:
            f.write(b"".join(chunks))
        return url
    return None


def _pick_slides(which: Optional[list[int]], count: int) -> list[int]:
    """1-based slide numbers: all (up to MAX_FRAMES) when which is empty."""
    if not which:
        return list(range(1, min(count, MAX_FRAMES) + 1))
    picked = sorted(set(which))
    if len(picked) > MAX_FRAMES:
        raise UserError(f"Pass at most {MAX_FRAMES} slide numbers (got {len(picked)}).")
    wrong = [n for n in picked if n < 1 or n > count]
    if wrong:
        raise UserError(f"There is no slide {wrong[0]}: this slideshow has {count} slide{'s' if count != 1 else ''}.")
    return picked


def slides(url: str, which: Optional[list[int]], tag: str) -> tuple[dict, list[tuple[int, bytes, str]]]:
    """(post summary with slide_count, [(slide number, jpeg, full-size link)]) for a
    TikTok slideshow: each jpeg FRAME_WIDTH on its longest side; the link is TikTok's own
    signed image link (it expires after a few hours). Call cleanup(tag) after."""
    url = normalise_url(url)
    if not _is_tiktok_photo(url):
        raise UserError("Not a TikTok slideshow. For a video, pass timestamps to get_frames.")
    meta = probe(url, tag, allow_photo=True)
    parts = urlparse(_tiktok_photo_as_video(url))
    try:
        page = requests.get(f"https://{parts.hostname}{parts.path}", headers=_TIKTOK_HEADERS, timeout=15)
    except requests.RequestException:
        raise UserError(_NO_SLIDES)
    images = _slide_urls(page.text)
    out = []
    for n in _pick_slides(which, len(images)):
        path = os.path.join(TMP, f"{tag}-s{n}")
        full = _fetch_slide(images[n - 1], path)
        if not full:
            raise UserError(f"TikTok refused slide {n}. Try again in a minute.")
        jpg = _grab(path, 0)
        if jpg is None:
            raise UserError(f"Couldn't read slide {n}.")
        out.append((n, jpg, full))
    meta["slide_count"] = len(images)
    return meta, out


def cleanup(job_id: str) -> None:
    for path in glob.glob(os.path.join(TMP, f"{glob.escape(job_id)}*")):
        _rm(path)


def _rm(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------- errors

_SIGN_IN = "The platform asked for a sign-in or bot check."
_ERROR_RULES = [
    (("unsupported url",), "This site or URL isn't supported by yt-dlp."),
    (("sign in to confirm", "not a bot", "confirm you", "rate-limit reached", "login required",
      "requested content is not available", "--cookies"), _SIGN_IN),
    (("private video", "this video is private", "video unavailable", "has been removed",
      "no longer available", "not found", "http error 404", "deleted", "account has been terminated"),
     "The video is private, removed or unavailable."),
    (("not available in your country", "geo restrict", "blocked it in your country"),
     "The video is geo-blocked from the server's location."),
    (("no video formats", "requested format is not available", "no audio", "unable to obtain file audio codec",
      "there's no video in this"),
     "No downloadable audio in this post (photo post or image-only)."),
    (("live event will begin", "is live", "premieres in"),
     "Live streams and upcoming premieres can't be transcribed."),
]


_SITE_NAMES = {"youtube": "YouTube", "tiktok": "TikTok", "instagram": "Instagram",
               "facebook": "Facebook", "x": "X", "vimeo": "Vimeo"}


def _cookie_problem(site: str, why: str) -> UserError:
    """A failure the server admin fixes, not the user: say so to both."""
    log.warning(f"[cookies] {site}: {why} Export a fresh cookies.txt (from a private browser "
                "window, then close it) and restart the server.")
    return UserError(f"{why} Ask the server's admin to refresh its cookies.txt.")


def _failure(exc: Exception, target: str, used_cookies: bool, stale: bool) -> UserError:
    """humanise, plus whether the server's cookies are to blame: the user can't fix
    those, and the admin needs to know they should."""
    site = _site(target)
    if used_cookies and stale:
        return _cookie_problem(site, f"The server's {site} cookies have expired, so {site} refused the request.")
    if "http error 403" in str(exc).lower():
        # Read the details fine, refused the file (TikTok's CDN does this without valid
        # cookies). Keep yt-dlp's words for the admin.
        log.warning(f"[yt-dlp] {site} refused the download: {log_text(exc, 300)}")
        if used_cookies:
            return _cookie_problem(site, f"{site} refused the download (HTTP 403) even with the server's "
                                         f"cookies: its {site} cookies are probably missing or expired.")
        return UserError(f"{site} refused the download (HTTP 403). This is often temporary: try again "
                         "in a few minutes. If it keeps happening, ask the server's admin to add "
                         f"{site} cookies to its cookies.txt or update yt-dlp.")
    err = humanise(exc)
    if str(err) != _SIGN_IN:
        return err
    if used_cookies:
        return _cookie_problem(site, f"{site} asked for a sign-in or bot check, and the server's "
                                     "cookies didn't pass it: they have probably expired.")
    return UserError(f"{site} asked for a sign-in or bot check. That needs a signed-in session, and "
                     "this server has no cookies.txt. Ask the server's admin to add one.")


def humanise(exc: Exception) -> UserError:
    if isinstance(exc, UserError):
        return exc
    msg = str(exc).lower()
    for needles, friendly in _ERROR_RULES:
        if any(n in msg for n in needles):
            return UserError(friendly)
    # Not a known cause: keep yt-dlp's own words in the log, or nobody can tell what broke.
    log.warning(f"[yt-dlp] unrecognised error: {log_text(exc, 500)}")
    return UserError("Download failed. The platform may be blocking requests; try again later.")
