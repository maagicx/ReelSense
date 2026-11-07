"""yt-dlp download + ffmpeg transcode + URL normalisation.

Everything here is synchronous and runs on the server's worker pool.
Failures surface as UserError with a short message fit for a tool result.
"""
import base64
import glob
import html
import json
import math
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import parse_qs, quote_plus, urlparse

import requests
import yt_dlp

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
MAX_FRAMES = int(os.environ.get("MAX_FRAMES") or 10)
FRAME_WIDTH = int(os.environ.get("FRAME_WIDTH") or 768)
FRAME_SOURCE_HEIGHT = 720  # read frames from a stream no taller than this
# Fallback when ffmpeg can't read the stream itself (TikTok's CDN wants yt-dlp's TLS
# impersonation): yt-dlp downloads the whole video, up to this size.
FRAME_DOWNLOAD_MAX_MB = float(os.environ.get("FRAME_DOWNLOAD_MAX_MB") or 50)
MAX_COMMENTS = int(os.environ.get("MAX_COMMENTS") or 200)

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
    if "tiktok.com" in url and "is_from_webapp" not in url:
        url += ("&" if "?" in url else "?") + "is_from_webapp=1&sender_device=pc"
    m = _ABCNEWS_RE.match(url)
    if m:
        url = f"https://abcnews.go.com/video/embed?id={m.group(1)}"
    m = _ALJAZEERA_RE.match(url)
    if m:
        url = f"{m.group(1)}/program/{m.group(2)}"
    return url


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


# ---------------------------------------------------------------- yt-dlp

class _QuietLogger:
    """Swallow yt-dlp's console output; errors still arrive as exceptions."""
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


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


def _with_cookies(opts: dict, tag: str) -> Optional[str]:
    """yt-dlp rewrites the jar, and the mounted one is read-only and shared,
    so each run gets its own writable copy. Returns the copy's path."""
    if not (COOKIES_FILE and os.path.exists(COOKIES_FILE)):
        return None
    path = os.path.join(TMP, f"{tag}.cookies.txt")
    shutil.copyfile(COOKIES_FILE, path)
    opts["cookiefile"] = path
    return path


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


def probe(url: str, tag: str, with_key: bool = False) -> dict:
    """Metadata only, no download. Returns the summary dict; with_key adds video_key
    ("<extractor>:<id>", the same for every link to one video) for caching."""
    url = normalise_url(url)
    if _is_tiktok_photo(url):
        raise UserError("This is a TikTok photo post: there is no audio to transcribe.")
    opts = {**_base_opts(url), "skip_download": True}
    cookies = _with_cookies(opts, f"{tag}-probe")
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        raise humanise(exc) from exc
    finally:
        if cookies:
            _rm(cookies)
    if info.get("_type") == "playlist":
        raise UserError("That URL is a playlist or channel. Pass a single video link, "
                        "or use list_videos / channel_info.")
    out = _summary(info, url)
    if with_key and info.get("id"):
        out["video_key"] = f"{info.get('extractor_key') or out['platform']}:{info['id']}"
    return out


def _flat_entry(entry: dict) -> Optional[dict]:
    if entry.get("_type") == "playlist" or not entry.get("url"):
        return None  # a nested tab or sub-playlist, not a video
    published = _date(entry.get("upload_date")) or _ts_date(entry.get("timestamp"))
    out = {"url": entry.get("webpage_url") or entry["url"], "title": entry.get("title")}
    _put(out, "duration", entry.get("duration"))
    _put(out, "published", published)
    _put(out, "views", entry.get("view_count"))
    return out


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
    cookies = _with_cookies(opts, tag)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(target, download=False)
    except Exception as exc:
        raise humanise(exc) from exc
    finally:
        if cookies:
            _rm(cookies)
    if info.get("_type") != "playlist" and not allow_video:
        raise UserError("That URL is a single video. Pass a channel, playlist or profile link.")
    return info


def list_flat(target: str, limit: int, tag: str) -> tuple[list[dict], bool]:
    """Channel/playlist/profile URL or a yt-dlp search target ("ytsearchN:..."), listed
    without visiting each video. playlistend stops yt-dlp walking a huge channel.
    Returns (videos, dates_approximate): YouTube's flat listings carry no upload date,
    only "3 weeks ago" text, which approximate_date turns into a rough one."""
    limit = max(1, min(limit, MAX_LIST_ITEMS))
    is_search = target.startswith("ytsearch")
    if not is_search:
        target = youtube_channel_url(normalise_url(target))
    youtube = is_search or platform_of(target) == "youtube"
    extra = {"extractor_args": {"youtubetab": {"approximate_date": [""]}}} if youtube else {}
    info = _flat_extract(target, f"{tag}-list", playlistend=limit, **extra)
    entries = (_flat_entry(e) for e in info.get("entries") or [] if isinstance(e, dict))
    return [e for e in entries if e][:limit], youtube


def list_popular(target: str, limit: int, tag: str) -> tuple[list[dict], bool, dict]:
    """Most-viewed videos, ranked here: YouTube ignores sort=p in tab URLs and yt-dlp can't
    pick the Popular chip, so this reads up to MAX_POPULAR_SCAN newest entries per tab and
    sorts by views. A bare YouTube channel covers its Videos and Shorts tabs. Returns
    (videos, dates_approximate, {scanned, scan_note?})."""
    limit = max(1, min(limit, MAX_LIST_ITEMS))
    url = normalise_url(target)
    root = _YT_CHANNEL_RE.match(url)
    tabs = [f"{root.group(1)}/videos", f"{root.group(1)}/shorts"] if root else [url]
    youtube = platform_of(url) == "youtube"
    extra = {"extractor_args": {"youtubetab": {"approximate_date": [""]}}} if youtube else {}
    videos, capped = [], False
    for i, tab in enumerate(tabs):
        try:
            info = _flat_extract(tab, f"{tag}-pop{i}", playlistend=MAX_POPULAR_SCAN, **extra)
        except UserError:
            if i == 0:
                raise
            continue  # a channel without a Shorts tab
        entries = [e for e in (_flat_entry(x) for x in info.get("entries") or [] if isinstance(x, dict)) if e]
        capped |= len(entries) >= MAX_POPULAR_SCAN
        for e in entries:
            if "/shorts/" in e["url"]:
                e["short"] = True
        videos += entries
    meta = {"scanned": len(videos)}
    if capped:
        meta["scan_note"] = f"Only the newest {MAX_POPULAR_SCAN} per tab were scanned; older videos aren't ranked."
    if not any("views" in v for v in videos):
        meta["scan_note"] = "This platform gave no view counts, so the order is the platform's own."
    videos.sort(key=lambda v: v.get("views") or 0, reverse=True)
    return videos[:limit], youtube, meta


# YouTube search "upload date" filter: name -> (protobuf enum in the sp= param, days).
# YouTube dropped sort-by-date (and yt-dlp its ytsearchdate: prefix), so "latest" is
# this filter plus a local sort. The filter is loose: once real matches run out YouTube
# pads with older videos, which the days cutoff removes.
UPLOAD_WINDOWS = {"hour": (1, 1), "day": (2, 1), "week": (3, 7), "month": (4, 31), "year": (5, 366)}


def search(query: str, limit: int, within: Optional[str], tag: str) -> tuple[list[dict], bool]:
    """YouTube keyword search; with `within`, only videos uploaded in that window, newest first."""
    limit = max(1, min(limit, MAX_LIST_ITEMS))
    if not within:
        return list_flat(f"ytsearch{limit}:{query}", limit, tag)
    code, days = UPLOAD_WINDOWS[within]
    # sp = {2: {1: upload date, 2: type video}}
    sp = base64.b64encode(bytes([0x12, 0x04, 0x08, code, 0x10, 0x01])).decode()
    url = f"https://www.youtube.com/results?search_query={quote_plus(query)}&sp={quote_plus(sp)}"
    # Only the top `limit` by relevance: deeper rows are weak fuzzy matches, which the
    # date sort below would otherwise push to the top. Fewer results beat wrong ones.
    videos, approx = list_flat(url, limit, tag)
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
    subscriber and verified fields; TikTok's extractor discards the profile stats, and
    its channel fields sit on the entries rather than the playlist, hence one entry.
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
    _put(out, "avatar", _avatar(info.get("thumbnails")) or (
        None if platform == "youtube" else page_avatar(out.get("url") or url)))
    if "subscribers" not in out:
        out["note"] = f"Channel statistics are not available for {platform} via yt-dlp."
    return out


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


def page_avatar(url: str) -> Optional[str]:
    """Profile picture from the profile page, for platforms whose yt-dlp extractor has
    none: TikTok's embedded page JSON (avatarThumb, ~4 KB), else og:image. Best effort:
    None on any failure. TikTok's link is signed and expires."""
    parts = urlparse(url)
    name = parts.path.strip("/").split("/")[0]
    if platform_of(url) not in _AVATAR_PAGES or not name:
        return None
    try:
        r = requests.get(f"https://{parts.hostname}/{name}", headers=_BROWSER_HEADERS, timeout=10)
        r.raise_for_status()
        if m := _TIKTOK_AVATAR_RE.search(r.text):
            found = json.loads(f'"{m.group(1)}"')
        elif m := _OG_IMAGE_RE.search(r.text):
            found = html.unescape(m.group(1) or m.group(2))
        else:
            return None
    except (requests.RequestException, ValueError):
        return None
    return found if found.startswith("https://") else None


def image_data_uri(url: str) -> Optional[str]:
    """An image as a data: URI, for the card's _meta when the card can't load the URL
    itself (signed links that expire, CDNs that refuse other sites). None if it fails,
    isn't an image or is over MAX_CARD_IMAGE_BYTES."""
    if not url.startswith("https://"):
        return None
    try:
        with requests.get(url, headers=_BROWSER_HEADERS, timeout=10, stream=True) as r:
            r.raise_for_status()
            kind = r.headers.get("content-type", "").split(";")[0].strip().lower()
            if not kind.startswith("image/"):
                return None
            body = r.raw.read(MAX_CARD_IMAGE_BYTES + 1, decode_content=True)
    except (requests.RequestException, OSError):
        return None
    if not body or len(body) > MAX_CARD_IMAGE_BYTES:
        return None
    return f"data:{kind};base64,{base64.b64encode(body).decode()}"


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
    cookies = _with_cookies(opts, f"{tag}-comments")
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        raise humanise(exc) from exc
    finally:
        if cookies:
            _rm(cookies)
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


def download_audio(url: str, job_id: str, section: Optional[dict] = None, music: bool = False) -> str:
    """Download best audio and transcode to 16 kHz mono 64 kbps MP3 (speech), or with
    music=True 44.1 kHz mono 128 kbps (what fingerprinting wants). Returns its path.
    section {start, end} downloads only that range (yt-dlp seeks; the rest isn't fetched)."""
    url = normalise_url(url)
    # TikTok lists the post's sound as an audio-only format "audio" from its music CDN:
    # with a licensed track attached that is the whole song (187 s of Mozart for a 50 s
    # video with a voiceover), not the video's own audio, so take the audio from the video.
    fmt = "bestaudio[format_id!=audio]/best" if platform_of(url) == "tiktok" else "bestaudio/best"
    opts = {
        **_base_opts(url),
        "format": fmt,
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
    _with_cookies(opts, job_id)  # cleaned up with the rest of {job_id}.* by cleanup()
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)
    except Exception as exc:
        raise humanise(exc) from exc

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
    cookies = _with_cookies(opts, f"{tag}-frames")
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        raise humanise(exc) from exc
    finally:
        if cookies:
            _rm(cookies)
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
    _with_cookies(opts, tag)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)
    except Exception as exc:
        raise humanise(exc) from exc
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


def cleanup(job_id: str) -> None:
    for path in glob.glob(os.path.join(TMP, f"{glob.escape(job_id)}*")):
        _rm(path)


def _rm(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------- errors

_ERROR_RULES = [
    (("unsupported url",), "This site or URL isn't supported by yt-dlp."),
    (("sign in to confirm", "not a bot", "confirm you", "rate-limit reached", "login required",
      "requested content is not available", "--cookies"),
     "The platform asked for a login or bot check. The server's cookies.txt needs refreshing."),
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


def humanise(exc: Exception) -> UserError:
    if isinstance(exc, UserError):
        return exc
    msg = str(exc).lower()
    for needles, friendly in _ERROR_RULES:
        if any(n in msg for n in needles):
            return UserError(friendly)
    return UserError("Download failed. The platform may be blocking requests; try again later.")
