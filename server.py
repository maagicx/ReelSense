"""transcribe-mcp: FastMCP server exposing video transcription (yt-dlp + ffmpeg + Deepgram).

Auth: OAuth sign-in through the provider chosen by AUTH_PROVIDER (Google by default, or
GitHub, any OpenID Connect provider, or Microsoft Entra ID), with an admin allowlist in
ALLOWED_USERS. See signin.py.
"""
import asyncio
import base64
import hashlib
import importlib.util
import json
import logging
import os
import sys
import threading
import time
import uuid
import warnings
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import uvicorn
import yt_dlp

# authlib (loaded by FastMCP's OAuth code) warns at import that its httpx module is deprecated.
# It's inside the library, not ours, and harmless; keep it out of the startup log.
# authlib.deprecate adds its own "always show" filter when imported, and the newest filter
# wins, so import it first and add ours after.
import authlib.deprecate  # noqa: E402,F401
warnings.filterwarnings("ignore", message=r"The httpx module is deprecated")

from fastmcp import Context, FastMCP
from fastmcp.apps import AppConfig, ResourceCSP, ResourcePermissions
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware
from fastmcp.tools.tool import ToolResult
from fastmcp.utilities.types import Image
from mcp.types import Icon, TextContent
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, PlainTextResponse

import consent_page
import deepgram
import embeddings
import media
import music_id
import on_screen
import pipeline
import signin
from media import UserError
from music_id import cache as music_cache


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name) or default)


def _fail(msg: str) -> None:
    print(f"FATAL: {msg}", file=sys.stderr)
    sys.exit(1)


PORT = _env_int("PORT", 8010)
CONCURRENCY = _env_int("CONCURRENCY", 2)
MAX_DURATION_S = _env_int("MAX_DURATION_S", 7200)
MAX_WAIT_S = _env_int("MAX_WAIT_S", 600)
MAX_JOBS_PER_HOUR = _env_int("MAX_JOBS_PER_HOUR", 20)
MAX_META_CALLS_PER_HOUR = _env_int("MAX_META_CALLS_PER_HOUR", 60)
JOB_RESULT_TTL_S = _env_int("JOB_RESULT_TTL_S", 3600)
MAX_MUSIC_FULL_PER_HOUR = _env_int("MAX_MUSIC_FULL_PER_HOUR", 3)
# The end-user setup guide (connecting Claude or ChatGPT), returned by help. Empty = not shown.
SETUP_GUIDE_URL = os.environ.get("SETUP_GUIDE_URL", "").strip()
MAX_SPOTIFY_PER_HOUR = _env_int("MAX_SPOTIFY_PER_HOUR", 60)  # each of find_on_spotify / find_on_apple_music: free, own cap
MUSIC_FULL_MAX_S = _env_int("MUSIC_FULL_MAX_S", 3600)
# Server-wide AudD requests per calendar month (UTC), so plan overage can't run away.
# 0 = no limit. Counted from what each call sends; check against the AudD dashboard.
AUDD_MAX_REQUESTS_PER_MONTH = _env_int("AUDD_MAX_REQUESTS_PER_MONTH", 0)
# For the cost a full scan quotes before it runs: AudD Indie's price per request over the plan.
AUDD_PRICE_PER_REQUEST = float(os.environ.get("AUDD_PRICE_PER_REQUEST") or 0.005)
MUSIC_CLIP_S = 15  # length of each quick-mode sample
MUSIC_WHOLE_MAX_S = 600  # quick mode downloads videos up to this long whole, longer ones per clip
MUSIC_HIT_TTL_S = 90 * 86400
MUSIC_MISS_TTL_S = 7 * 86400  # shorter: the catalogue grows, a miss may match later
# Browser impersonation for TikTok and other bot-protected sites; installed via the
# yt-dlp[curl-cffi] extra in the Dockerfile. Reported by health so a bad build shows.
IMPERSONATION = importlib.util.find_spec("curl_cffi") is not None
HEARTBEAT_S = 20  # keep bytes flowing: Cloudflare drops responses idle for 100 s
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()]
# Proxies whose X-Forwarded-For/-Proto are believed: by default only private addresses,
# which is where a tunnel or proxy container (or Docker's port forward) connects from.
FORWARDED_ALLOW_IPS = os.environ.get(
    "FORWARDED_ALLOW_IPS", "127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7").strip()
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
MCP_JWT_SIGNING_KEY = os.environ.get("MCP_JWT_SIGNING_KEY", "").strip()
# Where MCP clients may be sent back after sign-in: claude.ai, ChatGPT (one path per
# connector), and loopback for Claude Code and other desktop clients.
CLIENT_REDIRECTS = [
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "https://chatgpt.com/connector/oauth/*",
    "http://localhost/*",
    "http://127.0.0.1/*",
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stdout)
log = logging.getLogger("transcribe-mcp")
for _noisy in ("mcp", "fastmcp", "httpx"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


if not deepgram.API_KEY:
    _fail("DEEPGRAM_API_KEY is not set")
for _var in ("PUBLIC_URL", "MCP_JWT_SIGNING_KEY"):
    if not os.environ.get(_var, "").strip():
        _fail(f"{_var} is not set")
for _err in signin.config_errors():
    _fail(_err)
if not PUBLIC_URL.startswith("https://") and "localhost" not in PUBLIC_URL:
    _fail("PUBLIC_URL must be the public https:// address, e.g. https://mcp.example.com")
if len(MCP_JWT_SIGNING_KEY) < 32:
    # Signs the tokens handed to clients and keys the encrypted session store. Kept separate
    # from the provider's client secret so rotating that secret doesn't sign everyone out.
    _fail("MCP_JWT_SIGNING_KEY must be 32+ random chars")


# ---------------------------------------------------------------- jobs

class Jobs:
    """In-memory job store with TTL eviction, plus per-user hourly caps."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}
        self._hits: dict[str, deque] = {}

    def _evict(self, now: float) -> None:
        for jid in [j for j, v in self._jobs.items() if v.get("expires", now + 1) <= now]:
            del self._jobs[jid]

    def _recent(self, user: str, bucket: str, now: float) -> deque:
        hits = self._hits.setdefault(f"{bucket}:{user}", deque())
        while hits and hits[0] <= now - 3600:
            hits.popleft()
        return hits

    def take_slot(self, user: str, bucket: str = "jobs", limit: int = MAX_JOBS_PER_HOUR) -> bool:
        now = time.time()
        with self._lock:
            hits = self._recent(user, bucket, now)
            if len(hits) >= limit:
                return False
            hits.append(now)
            return True

    def refund_slot(self, user: str, bucket: str = "jobs") -> None:
        """Give back the newest slot, for a call that turned out to cost nothing (a cache hit)."""
        with self._lock:
            hits = self._recent(user, bucket, time.time())
            if hits:
                hits.pop()

    def used(self, user: str, bucket: str = "jobs") -> int:
        """Slots this user has taken in the last hour."""
        with self._lock:
            return len(self._recent(user, bucket, time.time()))

    def create(self, user: str) -> str:
        jid = uuid.uuid4().hex[:16]
        with self._lock:
            self._evict(time.time())
            self._jobs[jid] = {"user": user, "status": "running", "started": time.time(),
                               "progress": media.Progress()}
        return jid

    def finish(self, jid: str, **fields) -> None:
        with self._lock:
            if jid in self._jobs:
                self._jobs[jid].update(fields, expires=time.time() + JOB_RESULT_TTL_S)

    def progress(self, jid: str) -> media.Progress:
        with self._lock:
            job = self._jobs.get(jid)
            return job["progress"] if job else media.Progress()

    def get(self, jid: str) -> Optional[dict]:
        with self._lock:
            self._evict(time.time())
            job = self._jobs.get(jid)
            return dict(job) if job else None

    def running(self) -> int:
        with self._lock:
            return sum(1 for j in self._jobs.values() if j["status"] == "running")


JOBS = Jobs()
POOL = ThreadPoolExecutor(max_workers=CONCURRENCY, thread_name_prefix="job")


def _fmt_s(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60} min" if seconds >= 120 else f"{seconds} s"


def _run_job(jid: str, name: str, url: str, language: Optional[str],
             start: Optional[float] = None, end: Optional[float] = None,
             chapter: Optional[str] = None) -> dict:
    t0 = time.time()
    result: dict = {}
    cached = failed = False
    log.info(f"[{jid}] start {name} transcribe {media.clip(url, 300)}")
    progress = JOBS.progress(jid)
    token = media.track(progress)
    try:
        result = pipeline.transcribe_cached(url, jid, language, start, end, chapter, MAX_DURATION_S)
        cached = result.pop("cached", False)
        result = {**result, "steps": list(progress.steps), "status": "done"}
        JOBS.finish(jid, status="done", result=result)
        return result
    except Exception as exc:
        err = str(exc) if isinstance(exc, UserError) else "Internal error while transcribing."
        failed = True
        if isinstance(exc, UserError):
            log.info(f"[{jid}] failed: {err}")
        else:
            log.exception(f"[{jid}] unexpected failure")
        JOBS.finish(jid, status="error", error=err)
        return {"status": "error", "error": err, "steps": list(progress.steps)}
    finally:
        media.untrack(token)
        media.cleanup(jid)
        log.info(
            f"[mcp] {name} transcribe {result.get('platform', media.platform_of(url))} "
            f"{result.get('duration') or 0}s{' cached' if cached else ''}{' error' if failed else ''} "
            f"{int((time.time() - t0) * 1000)}ms"
        )


# ---------------------------------------------------------------- MCP

consent_page.install()  # branded "Allow Claude to use ReelSense?" page
auth = signin.build(PUBLIC_URL, MCP_JWT_SIGNING_KEY, CLIENT_REDIRECTS)

mcp = FastMCP(
    "transcribe",
    instructions=(
        "Transcribe videos from YouTube, TikTok, Instagram, Facebook, X, Vimeo, news sites "
        "and anything else yt-dlp supports. Call transcribe_video with the link for questions "
        "about what is said. For questions about what is visible (account labels, settings, "
        "prices, charts, slides, code), call find_on_screen instead: one call finds the "
        "moments the topic is spoken about and returns a frame from each. "
        "When channel details are wanted too, call channel_info first (with the channel or "
        "the video link), then transcribe_video, so the channel card shows above the video. "
        "To name the song or music playing in a video, call identify_music. To get a link "
        "for a track by name, call find_on_spotify (the default); call find_on_apple_music "
        "only when the user asks for Apple Music. "
        "If the user asks what this connector can do or how to use it, call help."
    ),
    auth=auth,
    website_url=PUBLIC_URL,
    icons=[
        Icon(src=f"{PUBLIC_URL}/icon.png", mimeType="image/png", sizes=["256x256"]),
        Icon(src=f"{PUBLIC_URL}/icon.svg", mimeType="image/svg+xml", sizes=["any"]),
    ],
)


def _claims() -> dict:
    """Claims of the signed-in caller (sub, email, name; login on GitHub; oid, groups on Entra)."""
    token = get_access_token()
    claims = (token.claims or {}) if token else {}
    return {**claims, **(claims.get("upstream_claims") or {})}


def _caller() -> tuple[str, str]:
    """(stable id for caps and job ownership, display name for logs and health)."""
    return signin.identity(_claims())


NOT_ALLOWED = "Not authorised: your account isn't on this server's list of users. Ask the server's admin."


class AllowlistGate(Middleware):
    """Refuse tools and cards to anyone signed in but not let in by ALLOWED_USERS (or, on
    Entra, ENTRA_ALLOWED_GROUP_ID). Signing in proves who someone is; this decides who may use it."""

    def _check(self):
        claims = _claims()
        if not signin.is_allowed(claims):
            log.info(f"[mcp] refused {signin.identity(claims)[1]}: not in ALLOWED_USERS")
            raise ToolError(NOT_ALLOWED)

    async def on_call_tool(self, context, call_next):
        self._check()
        return await call_next(context)

    async def on_read_resource(self, context, call_next):
        self._check()
        return await call_next(context)


if signin.gate_on():
    mcp.add_middleware(AllowlistGate())


# MCP App: clients that support io.modelcontextprotocol/ui render video_info,
# transcribe_video, get_transcript and channel_info as a card (thumbnail or avatar, title or name, stats); others get the JSON.
# The card loads images from these hosts directly. Any other image (TikTok's signed links,
# CDNs that refuse other sites) is fetched here and handed over inline in the result's
# _meta, which is for the UI rather than the model.
# Clients cache a ui:// resource by its URI, so the URI carries a hash of the page: a new
# card.html gets a new URI and is fetched fresh (an unchanged one keeps hitting the cache).
CARD_HTML = (Path(__file__).parent / "static" / "card.html").read_text(encoding="utf-8")
CARD = f"ui://transcribe/card-{hashlib.sha256(CARD_HTML.encode()).hexdigest()[:8]}.html"
CARD_IMAGE_HOSTS = ["https://i.ytimg.com", "https://yt3.ggpht.com", "https://yt3.googleusercontent.com"]
CARD_APP = AppConfig(resource_uri=CARD)
# Album art on identify_music's card: Spotify's image CDN, Apple's (is1-ssl… is5-ssl).
MUSIC_IMAGE_HOSTS = ["https://i.scdn.co", "https://*.mzstatic.com"]


# clipboardWrite: the video card's "Copy transcript" button (it falls back to selecting
# the text if a host doesn't grant it).
@mcp.resource(CARD, name="Media card", app=AppConfig(
    csp=ResourceCSP(resource_domains=CARD_IMAGE_HOSTS + MUSIC_IMAGE_HOSTS),
    permissions=ResourcePermissions(clipboard_write={})))
def card() -> str:
    return CARD_HTML


def _with_card_image(result: dict, image_key: str) -> ToolResult:
    """result as-is, plus _meta.image when its image is on a host the card can't load."""
    src = result.get(image_key)
    meta = None
    if isinstance(src, str) and not any(src.startswith(h + "/") for h in CARD_IMAGE_HOSTS):
        if data := media.image_data_uri(src):
            meta = {"image": data}
    return ToolResult(structured_content=result, meta=meta)


# ---- what the video tools send. One copy, for the model, as JSON text (no
# structuredContent, so nothing is duplicated whichever one a client feeds the model);
# the card's fields go in _meta.card, which the card reads and the model doesn't.

# Left out of the model's view unless full=true. The card still gets what it shows.
_FULL_ONLY = ("tags", "thumbnail", "channel_url", "channel_id", "age_limit", "availability", "heatmap")
_CARD_FIELDS = ("title", "channel", "channel_url", "channel_followers", "duration", "published",
                "platform", "url", "stats", "thumbnail", "live_status", "description",
                "auto_captions", "section")


def _transcript_lines(segments: list[dict]) -> str:
    """One line per sentence, "[m:ss] text", or "[m:ss] S1: text" with several speakers,
    and a blank line wherever the speaker changes."""
    lines, last = [], None
    for seg in segments:
        speaker = seg.get("speaker")
        if lines and speaker != last:
            lines.append("")
        label = f"S{speaker}: " if speaker is not None else ""
        lines.append(f"[{media._clock(seg['start'])}] {label}{seg['text']}")
        last = speaker
    return "\n".join(lines)


def _model_view(result: dict, full: bool = False, segments: bool = False) -> dict:
    """What Claude reads. The transcript is the timestamped lines (the segment array only
    with segments=true); rarely used metadata only with full=true."""
    if result.get("status") in ("error", "running"):
        return result
    view = dict(result)
    if view.get("segments"):
        view["transcript"] = _transcript_lines(view["segments"])
    if not segments:
        view.pop("segments", None)
    if not full:
        for key in _FULL_ONLY:
            view.pop(key, None)
        if view.get("live_status") == "not_live":
            view.pop("live_status")
        if view.get("description"):
            view["description"] = media.clip(view["description"], media.MAX_DESCRIPTION_CHARS)
    return view


def _video_result(result: dict, full: bool = False, segments: bool = False) -> ToolResult:
    view = _model_view(result, full, segments)
    meta: dict = {}
    if result.get("status") not in ("error", "running"):
        card = {k: result[k] for k in _CARD_FIELDS if k in result}
        if card.get("description"):
            card["description"] = media.clip(card["description"], 500)
        if view.get("transcript"):
            # For the card's "Copy transcript" button: the same lines Claude reads, so the
            # user can copy them whether or not Claude quotes them in chat.
            card["transcript"] = view["transcript"]
        meta["card"] = card
        src = result.get("thumbnail")
        if isinstance(src, str) and not any(src.startswith(h + "/") for h in CARD_IMAGE_HOSTS):
            if data := media.image_data_uri(src):
                meta["image"] = data
    text = json.dumps(view, ensure_ascii=False, separators=(",", ":"))
    return ToolResult(content=[TextContent(type="text", text=text)], meta=meta or None)


@mcp.tool(app=CARD_APP, output_schema=None)
async def transcribe_video(url: str, ctx: Context, language: Optional[str] = None,
                           start: Optional[str | float] = None, end: Optional[str | float] = None,
                           chapter: Optional[str] = None, full: bool = False,
                           segments: bool = False) -> dict:
    """Transcribe a video from its URL. Returns everything video_info does (and shows the
    same card) plus the transcript, so don't also call video_info for the same video.
    The transcript is one line per sentence, "[m:ss] text"; with more than one voice it is
    "[m:ss] S1: text" with a blank line at each change of speaker, and speakers gives the
    count: work out who is who from what they say when you can. When saving it as a
    Markdown file, make each timestamped line a bullet ("- [0:00] text"), because Markdown
    joins consecutive lines into one paragraph. segments=true adds the
    exact [{start,end,text,speaker?}] array; full=true adds the rest of the metadata
    (tags, thumbnail, channel ids, heatmap) and the whole description. For YouTube, always
    tell the user whether it has auto captions (auto_captions). If channel details are
    wanted too, call channel_info first (it accepts the video link) so its card shows
    above the video's. `language` is an optional BCP-47 code (e.g. "en", "es"); omit it
    to auto-detect. To transcribe only part of a video pass start and/or end (seconds or
    h:mm:ss, e.g. "1:02:30") or a chapter title; only that part is downloaded, the
    duration limit applies to its length (so parts of long streams work), and times stay
    on the full video's clock. Long videos may return {job_id, status:"running"}: then
    call get_transcript(job_id) with the same full/segments. steps lists what the server
    did, with timings (and a retry with cookies, if one was needed): use it to tell the
    user why a call failed or was slow."""
    try:
        start_s, end_s = media.parse_time(start), media.parse_time(end)
    except UserError as exc:
        return {"status": "error", "error": str(exc)}
    uid, name = _caller()
    if not JOBS.take_slot(uid):
        return {"status": "error", "error": f"Limit of {MAX_JOBS_PER_HOUR} transcriptions per hour reached."}
    jid = JOBS.create(uid)
    fut: Future = POOL.submit(_run_job, jid, name, url, language, start_s, end_s,
                              (chapter or "").strip() or None)
    result = await _await_job(fut, ctx, JOBS.progress(jid))
    if result is None:
        return {
            "job_id": jid,
            "status": "running",
            "message": "Still transcribing. Call get_transcript with this job_id in a minute.",
        }
    return await asyncio.to_thread(_video_result, result, full, segments)


async def _await_job(fut: Future, ctx: Context, progress: media.Progress) -> Optional[dict]:
    """A transcription job's result, or None if it's still running after MAX_WAIT_S. Sends
    what the job is doing every HEARTBEAT_S meanwhile, which also keeps the connection
    alive (clients that show MCP log messages show these)."""
    waiter = asyncio.wrap_future(fut)
    started = time.monotonic()
    deadline = started + MAX_WAIT_S
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            return await asyncio.wait_for(asyncio.shield(waiter), min(HEARTBEAT_S, remaining))
        except asyncio.TimeoutError:
            await ctx.info(f"{progress.status()}… {int(time.monotonic() - started)}s")


@mcp.tool(app=CARD_APP, output_schema=None)
async def get_transcript(job_id: str, full: bool = False, segments: bool = False) -> dict:
    """Fetch the result of a transcribe_video job that was still running. full and
    segments work as on transcribe_video."""
    job = JOBS.get(job_id)
    if not job or job["user"] != _caller()[0]:
        return {"status": "error", "error": "Unknown or expired job_id."}
    if job["status"] == "done":
        return await asyncio.to_thread(_video_result, job["result"], full, segments)
    if job["status"] == "error":
        return {"status": "error", "error": job["error"], "steps": list(job["progress"].steps)}
    return {"job_id": job_id, "status": "running", "elapsed_s": int(time.time() - job["started"]),
            "now": job["progress"].status(), "steps": list(job["progress"].steps)}


@mcp.tool(app=CARD_APP, output_schema=None)
async def video_info(url: str, full: bool = False) -> dict:
    """Video metadata only (title, channel, duration, date, platform, stats, chapters,
    description cut to 500 characters, most_replayed). No transcription. full=true adds
    tags, thumbnail, channel ids, the heatmap and the whole description. Skip it when
    transcribing: transcribe_video returns the same.
    For YouTube, always tell the user whether it has auto captions (auto_captions)."""
    (uid, name), t0 = _caller(), time.time()
    if not JOBS.take_slot(uid, "meta", MAX_META_CALLS_PER_HOUR):
        return {"status": "error", "error": f"Limit of {MAX_META_CALLS_PER_HOUR} info/list/search/channel calls per hour reached."}
    try:
        info = await asyncio.to_thread(media.probe, url, f"info-{uuid.uuid4().hex[:8]}")
    except UserError as exc:
        info = {"status": "error", "error": str(exc)}
    result = await asyncio.to_thread(_video_result, info, full)
    log.info(f"[mcp] {name} info {info.get('platform', '-')} {info.get('duration') or 0}s "
             f"{int((time.time() - t0) * 1000)}ms")
    return result


async def _listing(kind: str, platform: str, fetch, *args) -> dict:
    (uid, name), t0 = _caller(), time.time()
    if not JOBS.take_slot(uid, "meta", MAX_META_CALLS_PER_HOUR):
        return {"status": "error", "error": f"Limit of {MAX_META_CALLS_PER_HOUR} info/list/search/channel calls per hour reached."}
    try:
        videos, approx, *extra = await asyncio.to_thread(fetch, *args, f"{kind}-{uuid.uuid4().hex[:8]}")
        result = {"videos": videos, "count": len(videos), **(extra[0] if extra else {})}
        if approx and any("published" in v for v in videos):
            result["published_note"] = "YouTube dates are approximate (derived from 'N weeks ago')."
    except UserError as exc:
        videos, result = [], {"status": "error", "error": str(exc)}
    log.info(f"[mcp] {name} {kind} {platform} {len(videos)} items {int((time.time() - t0) * 1000)}ms")
    return result


@mcp.tool
async def list_videos(url: str, limit: int = 20, sort: str = "newest") -> dict:
    """List videos on a channel, playlist or profile URL: [{url, title, duration,
    published?, views?, short?}]. Max 100. sort="newest" (default) reads only the newest
    `limit`; a bare YouTube channel URL then lists its Videos tab (pass .../shorts or
    .../streams for those). For "most popular / most viewed / top videos" use
    sort="popular": it scans the whole channel (on YouTube both Videos and Shorts) and
    ranks by views; the result says how many were scanned."""
    order = sort.strip().lower()
    if order not in ("newest", "popular"):
        return {"status": "error", "error": 'sort must be "newest" or "popular".'}
    fetch = media.list_popular if order == "popular" else media.list_flat
    return await _listing("list", media.platform_of(url), fetch, url, limit)


@mcp.tool
async def search_videos(query: str, limit: int = 10, platform: str = "youtube",
                        uploaded_within: Optional[str] = None) -> dict:
    """Search for videos by keywords: [{url, title, duration, published?, views?}]. YouTube only.
    Max 100. Results are by relevance; for the latest videos pass uploaded_within ("hour",
    "day", "week", "month" or "year"): only videos from that window, newest first."""
    if platform.strip().lower() != "youtube":
        return {"status": "error", "error": f"Search is not supported on {platform}; only youtube."}
    if not query.strip():
        return {"status": "error", "error": "Empty search query."}
    within = (uploaded_within or "").strip().lower() or None
    if within and within not in media.UPLOAD_WINDOWS:
        return {"status": "error", "error": f"uploaded_within must be one of: {', '.join(media.UPLOAD_WINDOWS)}."}
    return await _listing("search", "youtube", media.search, query.strip(), limit, within)


@mcp.tool(app=CARD_APP)
async def channel_info(url: str) -> dict:
    """Channel or profile details for a channel/profile link, or a video link (resolved to
    the channel that posted it): name, handle, channel_id, url, description, avatar
    (YouTube, TikTok and others whose profile page has one); on YouTube also subscribers
    and verified. Other platforms return what exists plus a note."""
    (uid, name), t0 = _caller(), time.time()
    if not JOBS.take_slot(uid, "meta", MAX_META_CALLS_PER_HOUR):
        return {"status": "error", "error": f"Limit of {MAX_META_CALLS_PER_HOUR} info/list/search/channel calls per hour reached."}
    try:
        result = await asyncio.to_thread(media.channel, url, f"ch-{uuid.uuid4().hex[:8]}")
    except UserError as exc:
        result = {"status": "error", "error": str(exc)}
    result = await asyncio.to_thread(_with_card_image, result, "avatar")
    log.info(f"[mcp] {name} channel {media.platform_of(url)} {int((time.time() - t0) * 1000)}ms")
    return result


def _at_time(url: str, t: float) -> str:
    """The video link opening at t seconds, where the platform supports it (YouTube)."""
    if media.platform_of(url) != "youtube":
        return url
    return f"{url}{'&' if '?' in url else '?'}t={int(t)}"


@mcp.tool(app=CARD_APP, description=(
    "Still images from a video at the given times (seconds or h:mm:ss), so you can see "
    "slides, on-screen text, charts, products or people that the transcript doesn't "
    f"describe. Up to {media.MAX_FRAMES} per call, longest side {media.FRAME_WIDTH} px (each "
    "image is roughly 450 tokens). Pick moments from transcript segment times or chapters. "
    "Each call uses one transcription slot of the hourly limit. If you're picking times "
    "based on speech, use find_on_screen instead."))
async def get_frames(url: str, timestamps: list[str | float]) -> ToolResult:
    try:
        times = sorted({media.parse_time(t) for t in timestamps if media.parse_time(t) is not None})
    except UserError as exc:
        return ToolResult(content=[TextContent(type="text", text=f"Error: {exc}")], is_error=True)
    if not times or len(times) > media.MAX_FRAMES:
        return ToolResult(content=[TextContent(type="text", text=(
            f"Error: pass 1 to {media.MAX_FRAMES} timestamps (got {len(times)})."))], is_error=True)
    (uid, name), t0 = _caller(), time.time()
    if not JOBS.take_slot(uid):
        return ToolResult(content=[TextContent(type="text", text=(
            f"Error: limit of {MAX_JOBS_PER_HOUR} jobs per hour reached."))], is_error=True)
    tag = f"fr-{uuid.uuid4().hex[:8]}"
    try:
        meta, grabbed = await asyncio.to_thread(media.frames, url, times, tag)
    except UserError as exc:
        log.info(f"[mcp] {name} frames {media.platform_of(url)} error {int((time.time() - t0) * 1000)}ms")
        return ToolResult(content=[TextContent(type="text", text=f"Error: {exc}")], is_error=True)
    finally:
        await asyncio.to_thread(media.cleanup, tag)
    head = (f"{meta.get('title') or 'Video'} ({meta.get('platform')}, {meta.get('url')}): "
            f"{len(grabbed)} frame(s).")
    content: list = [TextContent(type="text", text=head)]
    for t, jpg in grabbed:
        content.append(TextContent(type="text", text=f"Frame at {media._clock(t)}:"))
        content.append(Image(data=jpg, format="jpeg").to_image_content())
    log.info(f"[mcp] {name} frames {meta.get('platform')} {len(grabbed)} "
             f"{sum(len(j) for _, j in grabbed) // 1024}KB {int((time.time() - t0) * 1000)}ms")
    # Claude sees the image blocks in content; claude.ai strips those from what it passes
    # the card, so _meta carries each frame again as a data URI for the gallery.
    video_url = meta.get("url") or url
    card = {"title": meta.get("title"), "channel": meta.get("channel"), "url": video_url,
            "platform": meta.get("platform"), "duration": meta.get("duration"),
            "frames": [{"time": t, "url": _at_time(video_url, t),
                        "image": f"data:image/jpeg;base64,{base64.b64encode(jpg).decode()}"}
                       for t, jpg in grabbed]}
    return ToolResult(content=content, meta={"frames": card})


FALLBACK_TRANSCRIPT_CHARS = 4000
# With a Voyage key, find_on_screen matches by meaning as well as by keyword. The floor is
# the lowest cosine similarity that counts; [embed] log lines show the best per query, for tuning.
EMBED_MIN_SCORE = float(os.environ.get("EMBED_MIN_SCORE") or 0.45)
ON_SCREEN_MATCHER = (on_screen.HybridMatcher(embeddings.embed_query, embeddings.embed_lines, EMBED_MIN_SCORE)
                     if embeddings.configured() else on_screen.KeywordMatcher())
MATCH_METHOD = "meaning+keyword" if embeddings.configured() else "keyword"
MATCH_HINT = ("matching is by meaning as well as keywords, so describe the topic naturally"
              if embeddings.configured() else "matching is by keyword, so use words the speaker would say")


@mcp.tool(app=CARD_APP, description=(
    "Find what is on screen when a topic is mentioned in a video. Use this whenever a question "
    "about a video depends on visible detail (account labels, settings, prices, charts, slides, "
    "code) rather than only on what is said. Transcribes, finds the moments the query is spoken "
    "about, and returns those transcript lines with a frame from each moment. `query` is the "
    "topic in a few words (e.g. \"paper trading account\"); " + MATCH_HINT + ". Up to 10 frames (max_frames, default 5), each taken `offset` seconds "
    "(default 1) after the line starts, since what's shown often lags the speech. If nothing "
    "matches, says so, returns the transcript and evenly spaced frames instead (fallback: true): "
    "retry with other words, or use get_frames at times you pick from the transcript. Same "
    "platforms as transcribe_video; a video already transcribed isn't transcribed again. Each "
    "call uses one transcription slot of the hourly limit."))
async def find_on_screen(url: str, query: str, ctx: Context, max_frames: int = 5,
                         offset: float = 1.0) -> ToolResult:
    def error(msg: str) -> ToolResult:
        return ToolResult(content=[TextContent(type="text", text=f"Error: {msg}")], is_error=True)

    query = (query or "").strip()[:200]
    if not query:
        return error("query is empty: say what to look for, e.g. \"pricing table\".")
    try:
        max_frames = max(1, min(int(max_frames), media.MAX_FRAMES, 10))
        offset = max(0.0, float(offset))
    except (TypeError, ValueError):
        return error("max_frames must be a whole number and offset a number of seconds.")
    (uid, name), t0 = _caller(), time.time()
    if not JOBS.take_slot(uid):  # one slot for transcript and frames together
        return error(f"limit of {MAX_JOBS_PER_HOUR} transcriptions per hour reached.")
    jid = JOBS.create(uid)
    result = await _await_job(POOL.submit(_run_job, jid, name, url, None), ctx, JOBS.progress(jid))
    if result is None:
        text = json.dumps({"job_id": jid, "status": "running", "message": (
            "Still transcribing (a long video). Call find_on_screen again with the same arguments "
            "in a minute: the transcript is kept, so it won't be transcribed again.")})
        return ToolResult(content=[TextContent(type="text", text=text)])
    if result.get("status") == "error":
        steps = "; ".join(result.get("steps") or [])
        return error(result["error"] + (f" (steps: {steps})" if steps else ""))

    segments = result.get("segments") or []
    duration = result.get("duration") or (segments[-1]["end"] if segments else None)
    matches, fallback = await asyncio.to_thread(
        on_screen.find, query, segments, duration, max_frames, offset, ON_SCREEN_MATCHER)
    times = [m["frame_time"] for m in matches] or on_screen.fallback_times(duration, max_frames)
    tag = f"os-{uuid.uuid4().hex[:8]}"
    try:
        frame_meta, grabbed = await asyncio.to_thread(media.frames, url, times, tag)
    except UserError as exc:
        log.info(f"[mcp] {name} on_screen {result.get('platform')} frames error {int((time.time() - t0) * 1000)}ms")
        return error(str(exc))
    finally:
        await asyncio.to_thread(media.cleanup, tag)

    view = _model_view(result)
    transcript_lines = view.pop("transcript", "")
    view.pop("segments", None)
    view.pop("status", None)
    view.update(query=query, match_method=MATCH_METHOD, matches=matches, fallback=fallback)
    if fallback and not segments:
        view["note"] = ("No speech in this video, so there was nothing to match. The frames below "
                        "are evenly spaced through the video, not tied to the query.")
    elif fallback:
        view["note"] = (f"No transcript lines matched {query!r}. The frames below are evenly spaced "
                        "through the video, not tied to the query. Check the transcript for other "
                        "words to search for, or call get_frames at times you pick from it.")
        view["transcript"] = media.clip(transcript_lines, FALLBACK_TRANSCRIPT_CHARS)
    by_time = {m["frame_time"]: m["text"] for m in matches}
    content: list = [TextContent(type="text", text=json.dumps(view, ensure_ascii=False, separators=(",", ":")))]
    for t, jpg in grabbed:
        said = by_time.get(round(t, 2))
        label = (f"Frame at {media._clock(t)}: \"{media.clip(said, 300)}\"" if said
                 else f"Frame at {media._clock(t)} (fallback, evenly spaced):")
        content.append(TextContent(type="text", text=label))
        content.append(Image(data=jpg, format="jpeg").to_image_content())
    log.info(f"[mcp] {name} on_screen {result.get('platform')} "
             f"{'fallback' if fallback else f'{len(matches)} matches'} {len(grabbed)} frames "
             f"{int((time.time() - t0) * 1000)}ms")
    video_url = frame_meta.get("url") or result.get("url") or url
    card = {"title": result.get("title"), "channel": result.get("channel"), "url": video_url,
            "platform": result.get("platform"), "duration": result.get("duration"),
            "query": query, "fallback": fallback,
            "frames": [{"time": t, "url": _at_time(video_url, t),
                        **({"text": by_time[round(t, 2)]} if round(t, 2) in by_time else {}),
                        "image": f"data:image/jpeg;base64,{base64.b64encode(jpg).decode()}"}
                       for t, jpg in grabbed]}
    return ToolResult(content=content, meta={"frames": card})


@mcp.tool
async def get_comments(url: str, limit: int = 50) -> dict:
    """Top comments on a YouTube video: [{author, text, likes, published, is_pinned?}].
    Slow; counts against the hourly transcription cap. Max 200."""
    (uid, name), t0 = _caller(), time.time()
    if not JOBS.take_slot(uid):
        return {"status": "error", "error": f"Limit of {MAX_JOBS_PER_HOUR} jobs per hour reached."}
    try:
        items = await asyncio.to_thread(media.comments, url, limit, f"cm-{uuid.uuid4().hex[:8]}")
        result = {"comments": items, "count": len(items)}
    except UserError as exc:
        items, result = [], {"status": "error", "error": str(exc)}
    log.info(f"[mcp] {name} comments {media.platform_of(url)} {len(items)} items "
             f"{int((time.time() - t0) * 1000)}ms")
    return result


MUSIC_BUDGET_GONE = "This month's music lookup allowance on the server is used up. It resets on the 1st."


def _budget_left() -> Optional[int]:
    """AudD requests left this month, or None with no monthly limit set."""
    if not AUDD_MAX_REQUESTS_PER_MONTH:
        return None
    return max(0, AUDD_MAX_REQUESTS_PER_MONTH - music_cache.requests_this_month())


MUSIC_NO_MATCH = ("No fingerprint match. Likely a live, unreleased or amateur recording, or no "
                  "music at all; check post_credit, the caption and the comments.")


def _post_credit(meta: dict) -> Optional[str]:
    """What the post itself says about its music: the platform's music tag, the title and
    the start of the description, for Claude to compare with the fingerprint."""
    parts = []
    music = meta.get("music") or {}
    tag = " - ".join(x for x in (music.get("artist"), music.get("track")) if x)
    if tag:
        parts.append(f"Music tag: {tag}" + (f" (album {music['album']})" if music.get("album") else ""))
    if meta.get("title"):
        parts.append(f"Title: {meta['title']}")
    if meta.get("description") and meta.get("description") != meta.get("title"):
        parts.append(f"Description: {media.clip(meta['description'], 300)}")
    tags = [t.lstrip("#") for t in meta.get("tags") or [] if t and t.strip("#")][:15]
    if tags:
        parts.append("Hashtags: " + " ".join(f"#{t}" for t in tags))
    return "\n".join(parts) or None


# Platform tags that credit the uploader's own audio rather than a release.
ORIGINAL_SOUND = ("original sound", "original audio", "son original", "sonido original", "som original",
                  "suono originale", "originalton", "orijinal ses", "оригинальный звук", "original song")


def _specific_credit(meta: dict) -> Optional[tuple[str, str]]:
    """(artist, track) when the post's music tag names a release, None for "original
    sound" or a tag that just repeats the uploader's name."""
    music = meta.get("music") or {}
    artist, track = (music.get("artist") or "").strip(), (music.get("track") or "").strip()
    if not (artist and track):
        return None
    from music_id.spotify import norm
    if any(o in norm(track) for o in ORIGINAL_SOUND) or music_id.generic_artist(artist):
        return None
    if norm(track) in (norm(meta.get("channel")), norm(artist)):
        return None
    return artist, track


def _credited_track(result: dict, meta: dict) -> bool:
    """When there is no match or a doubtful one and the post names a specific track, look
    that track up on Spotify once and keep it as credited_track. True if result changed."""
    if "credit_checked" in result:
        return False
    credit = _specific_credit(meta)
    tracks = result.get("tracks") or []
    if result.get("mode") == "full" and tracks:
        return False
    if not credit or (tracks and not any(music_id.doubts(t, credit) for t in tracks)):
        return False
    from music_id import spotify
    if not spotify.configured():
        return False
    hits = spotify.search(artist=credit[0], title=credit[1], limit=1)
    result["credit_checked"] = True
    if hits:
        result["credited_track"] = hits[0]
    return True


def _sample_points(duration: float, start: Optional[float]) -> list[float]:
    """Where quick mode samples: start_seconds, else 10%, 40% and 70% in, each moved so a
    whole clip fits, and dropped if within 5 s of one already picked."""
    if start is not None:
        if duration and start >= duration:
            raise UserError(f"{media._clock(start)} is past the end of the video ({media._clock(duration)}).")
        return [start]
    if not duration:
        return [0.0]
    points: list[float] = []
    for p in (0.1, 0.4, 0.7):
        t = max(0.0, min(duration * p, duration - MUSIC_CLIP_S))
        if all(abs(t - q) > 5 for q in points):
            points.append(round(t, 1))
    return points


def _recording_year(track: dict) -> Optional[int]:
    """Year the recording was first registered: the ISRC's year digits (CC-XXX-YY-NNNNN),
    which a later compilation keeps, else the release date's year, which it doesn't."""
    isrc = (track.get("isrc") or "").replace("-", "")
    if len(isrc) == 12 and isrc[5:7].isdigit():
        yy = int(isrc[5:7])
        return 1900 + yy if yy >= 40 else 2000 + yy
    year = (track.get("release_date") or "")[:4]
    return int(year) if year.isdigit() else None


def _estimate(duration: float, head: dict, provider) -> dict:
    """What a full scan would cost, returned instead of scanning until the user agrees."""
    if duration > MUSIC_FULL_MAX_S:
        raise UserError(f"Video is {_fmt_s(duration)}; full scans are limited to "
                        f"{_fmt_s(MUSIC_FULL_MAX_S)}. Use mode=\"quick\" with start_seconds instead.")
    cost, left = provider.full_cost(duration), _budget_left()
    if left == 0:
        raise UserError(MUSIC_BUDGET_GONE)
    if left is not None and cost > left:
        raise UserError(f"A full scan of this video costs about {cost} music lookup requests and "
                        f"{left} are left on the server this month. Use mode=\"quick\" instead.")
    est = {"requests": cost, "cost_usd_if_over_plan": round(cost * AUDD_PRICE_PER_REQUEST, 2)}
    if left is not None:
        est["requests_left_this_month"] = left
    msg = (f"Not scanned yet. A full scan of this {_fmt_s(duration)} video uses about {cost} AudD requests"
           + (f" of the {left} left this month" if left is not None else "")
           + f" (about ${est['cost_usd_if_over_plan']:.2f} if beyond the plan's included requests). "
           "Tell the user and ask before scanning; if they agree, call identify_music again with "
           "mode=\"full\" and confirm=true. mode=\"quick\" names one track for 1 to 3 requests.")
    return {"status": "needs_confirmation", "mode": "full", "estimate": est, "message": msg, **head}


MUSIC_SERVICE_NAMES = {"audd": "AudD"}


def _track_name(track) -> str:
    return " - ".join(x for x in (track.artist, track.title) if x) or "a track"


def _identify(url: str, mode: str, start: Optional[float], tag: str, provider, confirm: bool = True) -> dict:
    t0 = time.time()
    media.doing("reading the video's details")
    meta = media.probe(url, tag, with_key=True)
    duration = meta.get("duration") or 0
    media.step(tag, f"read details: {meta.get('platform')}, {duration}s ({media.since(t0)})")
    service = MUSIC_SERVICE_NAMES.get(provider.name, provider.name)
    head = {"video": {k: meta[k] for k in ("title", "url", "platform", "duration") if meta.get(k)},
            "post_credit": _post_credit(meta)}
    key = f"{meta.get('video_key') or meta.get('url')}:{mode}:{int(start) if start is not None else '-'}"
    if cached := music_cache.get(key):
        stale = mode == "quick" and cached.get("notes", "").startswith("A fingerprint names a recording")
        if stale:  # cached before that note was kept for full scans only
            cached.pop("notes")
        arted = music_id.fill_art(cached.get("tracks") or [])  # cached before the card had art
        if _credited_track(cached, meta) or arted or stale:
            ttl = MUSIC_HIT_TTL_S if cached.get("tracks") else MUSIC_MISS_TTL_S
            music_cache.put(key, cached, ttl)
        media.step(tag, "music result from cache: no new lookup")
        return _assess({**cached, **head, "cached": True, "requests_used": 0}, meta)
    if mode == "full" and not confirm:
        return _estimate(duration, head, provider)
    used, tracks = 0, []
    if mode == "full":
        if duration > MUSIC_FULL_MAX_S:
            raise UserError(f"Video is {_fmt_s(duration)}; full scans are limited to "
                            f"{_fmt_s(MUSIC_FULL_MAX_S)}. Use mode=\"quick\" with start_seconds instead.")
        cost, left = provider.full_cost(duration), _budget_left()
        if left == 0:
            raise UserError(MUSIC_BUDGET_GONE)
        if left is not None and cost > left:
            raise UserError(f"A full scan of this video costs about {cost} music lookup requests and "
                            f"{left} are left on the server this month. Use mode=\"quick\" instead.")
        t0 = time.time()
        media.doing("downloading the audio")
        path = media.download_audio(url, tag, music=True)
        media.step(tag, f"downloaded audio: {media.file_mb(path)} ({media.since(t0)})")
        t0 = time.time()
        media.doing(f"scanning the whole audio with {service}")
        tracks = provider.identify_full(path)
        media.step(tag, f"{service} full scan: {len(tracks)} tracks, about {cost} requests ({media.since(t0)})")
        used = cost
        music_cache.add_requests(cost)
    else:
        whole, partial = None, False
        for i, t in enumerate(_sample_points(duration, start)):
            left = _budget_left()
            if left is not None and left < 1:
                if not used:
                    raise UserError(MUSIC_BUDGET_GONE)
                partial = True  # stopped early: not a real miss, so don't cache it as one
                media.step(tag, "stopped: the server's monthly music lookups ran out")
                break
            t1 = time.time()
            if duration and duration <= MUSIC_WHOLE_MAX_S:
                if not whole:
                    media.doing("downloading the audio")
                    whole = media.download_audio(url, tag, music=True)
                    media.step(tag, f"downloaded audio: {media.file_mb(whole)} ({media.since(t1)})")
                clip = media.cut_clip(whole, t, MUSIC_CLIP_S, os.path.join(media.TMP, f"{tag}-c{i}.mp3"))
            else:  # only these 15 s are fetched, so a 2 h video isn't downloaded for 45 s of it
                media.doing(f"downloading {MUSIC_CLIP_S} s from {media._clock(t)}")
                clip = media.download_audio(url, f"{tag}-c{i}", {"start": t, "end": t + MUSIC_CLIP_S}, music=True)
                media.step(tag, f"downloaded {MUSIC_CLIP_S} s from {media._clock(t)} ({media.since(t1)})")
            if not clip:
                media.step(tag, f"no audio at {media._clock(t)}, skipped")
                continue
            used += 1
            music_cache.add_requests(1)
            t1 = time.time()
            media.doing(f"asking {service} about the clip at {media._clock(t)}")
            tracks = provider.identify_clip(clip)
            media.step(tag, f"{service} lookup at {media._clock(t)}: "
                            f"{'matched ' + _track_name(tracks[0]) if tracks else 'no match'} ({media.since(t1)})")
            if tracks:
                for track in tracks:
                    track.offset_seconds = round(t)
                break
    music_id.fill_spotify(tracks)
    found = {"matched": bool(tracks), "provider": provider.name, "mode": mode,
             "tracks": [t.to_dict() for t in tracks]}
    notes = []
    if not tracks:
        notes.append(MUSIC_NO_MATCH)
        if mode == "quick" and partial:
            notes.append("Only part of the video was checked: the monthly allowance ran out.")
    elif mode == "full":
        notes.append("A fingerprint names a recording, not a composition: another performer's "
                     "recording of the same piece can match a live one.")
    if notes:
        found["notes"] = " ".join(notes)
    _credited_track(found, meta)
    if tracks or not (mode == "quick" and partial):
        music_cache.put(key, found, MUSIC_HIT_TTL_S if tracks else MUSIC_MISS_TTL_S)
    return _assess({**found, **head, "requests_used": used}, meta)


def _assess(result: dict, meta: dict) -> dict:
    """Marks tracks Claude should doubt. A recording made after the video was posted can't
    be what plays in it: a later remix of the same song (mixes) or a track sampling the
    video ("Me at the zoo"). Generic artists ("V.A"), no links at all, or a match that
    shares nothing with the post's music tag are doubtful too."""
    posted = (meta.get("published") or "")[:4]
    # A mix's tag names one track at most; every other track would "disagree" with it.
    credit = _specific_credit(meta) if result.get("mode") != "full" else None
    tracks, notes, later = [], [], False
    for t in result["tracks"]:
        t = {k: v for k, v in t.items() if k not in ("doubtful", "doubt_reasons")}
        if posted.isdigit() and (_recording_year(t) or 0) > int(posted):
            t["released_after_video"] = later = True
        if reasons := music_id.doubts(t, credit):
            t["doubtful"], t["doubt_reasons"] = True, reasons
        tracks.append(t)
    result["tracks"] = tracks
    if later:
        notes.append(f"Tracks marked released_after_video came out after the video was posted ({posted}), "
                     "so they can't be what plays in it: probably a later remix, cover or track that "
                     "samples the same audio. Prefer another match for that moment.")
    if any(t.get("doubtful") for t in tracks):
        notes.append("Tracks marked doubtful (see doubt_reasons) may be a compilation, cover or "
                     "namesake rather than what plays; weigh them against post_credit.")
    if result.get("credited_track"):
        notes.append("credited_track is the track the post's music tag names, found on Spotify. "
                     "Prefer it over a doubtful or missing fingerprint match.")
    if notes:
        result["notes"] = " ".join(filter(None, [result.get("notes"), *notes]))
    return result


def _music_result(result: dict) -> ToolResult:
    """The model reads the result as JSON text without album art; the card gets
    _meta.music, the same plus art."""
    if result.get("status") == "error":
        view, meta = result, None
    elif result.get("status") == "needs_confirmation":
        view = result
        meta = {"music": {k: result[k] for k in ("mode", "estimate", "message", "video") if k in result}}
    else:
        # A track the Spotify lookup ran for and didn't find says so, so a miss and a
        # lookup that never ran (Spotify not configured) don't look the same.
        tracks = [{**{k: v for k, v in t.items() if k != "spotify_searched"},
                   **({"spotify_not_found": True} if t.get("spotify_searched") and not t.get("spotify_url") else {})}
                  for t in result["tracks"]]
        missing = [t.get("title") or "?" for t in tracks if t.get("spotify_not_found")]
        if missing:
            note = (f"Not found on Spotify (searched by ISRC, then title and artist): {', '.join(missing[:5])}"
                    + (f" and {len(missing) - 5} more" if len(missing) > 5 else "") + ".")
            result = {**result, "notes": f"{result['notes']} {note}" if result.get("notes") else note}
        credited = result.get("credited_track")
        view = {k: v for k, v in result.items() if k != "credit_checked"}
        view["tracks"] = [{k: v for k, v in t.items() if k != "artwork"} for t in tracks]
        if credited:
            view["credited_track"] = {k: v for k, v in credited.items() if k != "artwork"}
        meta = {"music": {k: v for k, v in {**result, "tracks": tracks}.items()
                          if k in ("matched", "mode", "tracks", "video", "notes", "cached", "credited_track")
                          and v is not None}}
    text = json.dumps(view, ensure_ascii=False, separators=(",", ":"))
    return ToolResult(content=[TextContent(type="text", text=text)], meta=meta)


@mcp.tool(app=CARD_APP, output_schema=None, description=(
    "Identify the music playing in a video (TikTok, YouTube, Instagram and other yt-dlp "
    "sources) by audio fingerprint. Returns artist, track, album and links (Spotify first), "
    "plus whatever the post itself credits (post_credit), so you can compare them. Use when "
    "the user asks what song or piece is playing, or who performs it. Fingerprinting "
    "matches commercial recordings: live, unreleased or amateur performances often won't "
    "match, so check the post caption and comments as well. mode=\"quick\" (default) "
    "samples 15 s at a few points (or at start_seconds: seconds or h:mm:ss) and stops at "
    "the first match. mode=\"full\" scans the whole video for multiple tracks with their "
    f"times (mixes, compilations), for videos up to {MUSIC_FULL_MAX_S // 60} min and "
    f"{MAX_MUSIC_FULL_PER_HOUR} scans per hour. It costs far more (about 50 paid requests for "
    "30 min), so the first full call only returns the estimated cost (status "
    "\"needs_confirmation\") and spends nothing: tell the user the cost and ask, then call "
    "again with confirm=true only if they agree. A video already scanned returns its cached "
    "tracks straight away. Each lookup uses one slot of the hourly limit. Tracks marked "
    "doubtful (doubt_reasons: generic_artist such as \"V.A\", no_links, credit_disagrees, "
    "released_after_video) may be a compilation, cover or namesake; also judge whether the "
    "match suits the post (era, hashtags, genre). credited_track is the track the post's music "
    "tag names, found on Spotify: prefer it over a doubtful match. When the tag is only "
    "\"original sound\", use find_on_spotify with names from the caption or hashtags. "
    "steps lists what the server did, with timings: use it to tell the user why a call "
    "failed, was slow or found nothing."))
async def identify_music(url: str, ctx: Context, mode: str = "quick",
                         start_seconds: Optional[str | float] = None, confirm: bool = False) -> ToolResult:
    mode = (mode or "quick").strip().lower()
    if mode not in ("quick", "full"):
        return {"status": "error", "error": 'mode must be "quick" or "full".'}
    try:
        start = media.parse_time(start_seconds)
        provider = music_id.get_provider()
    except UserError as exc:
        return {"status": "error", "error": str(exc)}
    if not provider.configured():
        return {"status": "error", "error": "Music identification isn't configured on this server."}
    (uid, name), t0 = _caller(), time.time()
    estimate_only = mode == "full" and not confirm  # metadata only: no AudD request, no slot
    if not estimate_only and mode == "full" and not JOBS.take_slot(uid, "music_full", MAX_MUSIC_FULL_PER_HOUR):
        return {"status": "error", "error": f"Limit of {MAX_MUSIC_FULL_PER_HOUR} full music scans per hour reached. "
                                            "mode=\"quick\" still works."}
    if not estimate_only and not JOBS.take_slot(uid):
        return {"status": "error", "error": f"Limit of {MAX_JOBS_PER_HOUR} jobs per hour reached."}
    tag = f"mu-{uuid.uuid4().hex[:8]}"

    progress = media.Progress()

    def run() -> dict:
        token = media.track(progress)
        try:
            return {**_identify(url, mode, start, tag, provider, confirm), "steps": list(progress.steps)}
        except UserError as exc:
            log.info(f"[{tag}] failed: {exc}")
            return {"status": "error", "error": str(exc), "steps": list(progress.steps)}
        except Exception:
            log.exception(f"[{tag}] unexpected failure")
            return {"status": "error", "error": "Internal error while identifying the music.",
                    "steps": list(progress.steps)}
        finally:
            media.untrack(token)
            media.cleanup(tag)

    log.info(f"[{tag}] start {name} identify_music {mode} {media.clip(url, 300)}")

    waiter = asyncio.wrap_future(POOL.submit(run))
    started = time.monotonic()
    while True:  # heartbeats keep Cloudflare from dropping a long full scan
        try:
            result = await asyncio.wait_for(asyncio.shield(waiter), HEARTBEAT_S)
            break
        except asyncio.TimeoutError:
            await ctx.info(f"{progress.status()}… {int(time.monotonic() - started)}s")
    if result.get("cached") and not estimate_only:  # no AudD request: don't count it
        JOBS.refund_slot(uid)
        if mode == "full":
            JOBS.refund_slot(uid, "music_full")
    outcome = result["status"] if result.get("status") in ("error", "needs_confirmation") else (
        "cached" if result.get("cached") else f"{len(result['tracks'])} tracks {result['requests_used']} req")
    log.info(f"[mcp] {name} music {mode} {media.platform_of(url)} {outcome} {int((time.time() - t0) * 1000)}ms")
    return _music_result(result)


@mcp.tool(app=CARD_APP, output_schema=None, description=(
    "Search Spotify for a track and return candidate matches with links (artist, title, album, "
    "release date, ISRC, Spotify link). No audio fingerprinting and no paid requests. Give "
    "artist and title to get only tracks by that artist, or query for a free-text search. "
    "Use it to get a link for a track you already know, to check a post's credited track, or "
    "when identify_music finds no match or a doubtful one and the post's music tag is just "
    "\"original sound\": read the caption and hashtags in post_credit for song or artist "
    "names (e.g. \"Insomnia × New Religion\" is two tracks) and search each one. Say which "
    "results are your guess from the caption rather than a fingerprint match."))
async def find_on_spotify(query: Optional[str] = None, artist: Optional[str] = None,
                          title: Optional[str] = None, limit: int = 5) -> ToolResult:
    return await _track_search("spotify", query, artist, title, limit)


@mcp.tool(app=CARD_APP, output_schema=None, description=(
    "Search Apple Music for a track and return candidate matches with Apple Music links "
    "(artist, title, album, release date). Only use this when the user asks for Apple Music "
    "or for a track that find_on_spotify didn't find; Spotify is the default, so otherwise "
    "use find_on_spotify. Give artist and title to get only tracks by that artist, or query "
    "for a free-text search. No paid requests."))
async def find_on_apple_music(query: Optional[str] = None, artist: Optional[str] = None,
                              title: Optional[str] = None, limit: int = 5) -> ToolResult:
    return await _track_search("apple_music", query, artist, title, limit)


SEARCH_SERVICES = {"spotify": "Spotify", "apple_music": "Apple Music"}


async def _track_search(service: str, query: Optional[str], artist: Optional[str],
                        title: Optional[str], limit: int) -> ToolResult:
    from music_id import apple, spotify
    label_name = SEARCH_SERVICES[service]
    query, artist, title = ((x or "").strip()[:200] or None for x in (query, artist, title))
    if not (query or title):
        return {"status": "error", "error": "Give query, or artist and title."}
    if service == "spotify" and not spotify.configured():
        return {"status": "error", "error": "Spotify search isn't configured on this server."}
    uid, name = _caller()
    if not JOBS.take_slot(uid, service, MAX_SPOTIFY_PER_HOUR):
        return {"status": "error", "error": f"Limit of {MAX_SPOTIFY_PER_HOUR} {label_name} searches per hour reached."}
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 5
    tracks = await asyncio.to_thread((spotify if service == "spotify" else apple).search, query, artist, title, limit)
    label = query or " - ".join(x for x in (artist, title) if x)
    log.info(f"[mcp] {name} {service} search {len(tracks)} results")
    # The country searched: Spotify returns only versions playable there, Apple that store's catalogue.
    market = (spotify.MARKET if service == "spotify" else apple.COUNTRY).upper() or "any"
    result = {"query": label, "service": service, "market": market,
              "tracks": [{k: v for k, v in t.items() if k != "artwork"} for t in tracks]}
    if not tracks:
        result["notes"] = f"Nothing found on {label_name}. Try fewer words, or the artist and title separately."
    text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    return ToolResult(content=[TextContent(type="text", text=text)],
                      meta={"music": {"mode": "search", "service": service, "market": market,
                                      "query": label, "tracks": tracks}})


MEANING_HELP = ("- It finds the topic even when it's said in other words (\"pricing\" finds \"it costs £49 a "
                "month\", and misheard words often still match).\n" if embeddings.configured() else "")

HELP = f"""\
# ReelSense: what you can ask

**Transcribe a video.** Paste a link from YouTube, TikTok, Instagram, Facebook, X, Vimeo, \
a news site or most other video sites.
- "Transcribe https://youtu.be/... and summarise the main points"
- "Transcribe this in Spanish: <link>" (the language is normally detected automatically)
- "Transcribe 1:00:00 to 1:20:00 of <link>" or "Transcribe the chapter about <topic> of <link>": \
only that part is downloaded, and long streams work this way. Timestamps match the full video.
- Interviews and podcasts come back with speakers labelled (S1, S2); ask Claude \
to name them from the conversation.

**Everything at once.** Name the steps and Claude does them in order:
- "For <link>: show the channel card, then transcribe it, give me a 5-bullet summary and the \
key stats as a table, and save the full transcript as a Markdown file with timestamps." \
(Transcribing shows the video card by itself.)
- Add "and the top 10 comments" or "and list the channel's 5 latest videos" for more.

**Details without transcribing.** Title, channel, length, date, views, description, chapters, \
tags and, on YouTube, whether it has automatic captions. Shown as a card with the thumbnail in Claude.
- "What is this video about and how long is it? <link>"

**Browse and search.**
- "List the last 10 videos from https://www.youtube.com/@channel" (also playlists and TikTok profiles)
- "What are the most popular videos on https://www.youtube.com/@channel?" (ranks the whole \
channel, Shorts included, by views)
- "Search YouTube for <topic>, 5 results"
- "Latest YouTube videos about <topic> from the past month" (newest first; dates are approximate)

**Channel details.** Name, handle, description and profile picture, shown as a card in Claude; \
on YouTube also subscribers and verified status.
- "How many subscribers does https://www.youtube.com/@channel have?"

**See the video.** Stills at chosen moments (up to {media.MAX_FRAMES} at a time), for slides, \
on-screen text or charts the speech doesn't cover. Claude reads them, and in Claude they show as \
a gallery with "Open at" links to that moment.
- "Show me the slide at 12:30 in <link>" or "Grab frames at the start of each chapter of <link>"

**Find what's on screen when something is mentioned.** One step: Claude transcribes the video, \
finds the moments a topic is talked about and shows a still from each, with the line being said. \
For details you have to see: account names, settings, prices, charts, slides, code.
- "Which account is on screen when they talk about paper trading? <link>"
- "Show me the pricing slide from <link>"
{MEANING_HELP}- If the topic is never said, Claude tells you and shows evenly spaced stills instead.

**Identify the music.** Names the song or recording playing in a video by its audio \
fingerprint, with a Spotify link, and shows what the post itself credits so Claude can compare.
- "What song is playing in <link>?" or "Who performs the music at 0:45 in <link>?"
- "List every track in this mix: <link>" (a full scan, for videos up to {MUSIC_FULL_MAX_S // 60} min). \
Claude tells you what it will cost first and only scans if you say yes.
- Live, unreleased or amateur performances usually don't match: Claude then goes by the caption \
and comments.
- A weak match (a "V.A" compilation, no streaming link, or a song that doesn't agree with the post's \
music tag) is marked doubtful, and the track the post credits is looked up on Spotify alongside it.
- "Find <artist> - <title> on Spotify": a Spotify search with no fingerprint, also used when a post \
only says "original sound" but the caption or hashtags name the songs.
- "Find <artist> - <title> on Apple Music": the same on Apple Music, only when you ask for it \
(Spotify is the default).

**Comments (YouTube only).**
- "What are the top 20 comments on <link> saying?"

## Good to know
- Videos up to {MAX_DURATION_S // 3600} h, or a part of up to {MAX_DURATION_S // 3600} h of anything \
longer. Long ones keep transcribing in the background; just ask Claude to check on it.
- Can't be done: private or login-only videos, live streams and premieres, TikTok photo posts, \
listing Instagram profiles (single Instagram posts and reels work).
- Transcripts come from speech recognition (Deepgram), so names and jargon can be misspelled.
- To copy a whole transcript, use **Copy transcript** on the video card (or expand the tool call). \
Claude may not paste a creator's full script into the chat.
- Comments are slower. Comments, frame grabs and music lookups count towards your transcription limit; \
finding what's on screen counts once, for the transcript and its stills together. A music lookup \
answered from the cache (a video already identified) doesn't count.
- A video transcribed once is kept for a week, so asking about it again doesn't transcribe it again.
- Limits per person per hour: {MAX_JOBS_PER_HOUR} transcriptions, frame grabs, music lookups and \
comment requests ({MAX_MUSIC_FULL_PER_HOUR} of them full music scans), {MAX_META_CALLS_PER_HOUR} video info \
lookups, lists, searches and channel lookups, {MAX_SPOTIFY_PER_HOUR} Spotify searches and {MAX_SPOTIFY_PER_HOUR} \
Apple Music searches.
- You sign in with your {signin.LABEL} account. Access is granted by the server's admin.
"""


@mcp.tool(name="help")
def help_tool() -> dict:
    """How to use this connector: what it can do, example requests, limits, and how much of
    your hourly limit you have used. Call when the user asks for help or what it can do.
    For connecting Claude or ChatGPT (or a colleague's), point to setup_guide."""
    uid, name = _caller()
    return {
        "guide": HELP,
        "signed_in_as": name,
        "used_this_hour": {
            "transcriptions_frames_music_and_comments": f"{JOBS.used(uid)} of {MAX_JOBS_PER_HOUR}",
            "full_music_scans": f"{JOBS.used(uid, 'music_full')} of {MAX_MUSIC_FULL_PER_HOUR}",
            "lists_searches_and_channels": f"{JOBS.used(uid, 'meta')} of {MAX_META_CALLS_PER_HOUR}",
            "spotify_searches": f"{JOBS.used(uid, 'spotify')} of {MAX_SPOTIFY_PER_HOUR}",
            "apple_music_searches": f"{JOBS.used(uid, 'apple_music')} of {MAX_SPOTIFY_PER_HOUR}",
        },
        **({"setup_guide": SETUP_GUIDE_URL} if SETUP_GUIDE_URL else {}),
    }


@mcp.tool
def health() -> dict:
    """Server status: yt-dlp version, impersonation support, transcription model, queue depth,
    and who you are signed in as."""
    return {
        "user": _caller()[1],
        "yt_dlp": yt_dlp.version.__version__,
        "deepgram_model": deepgram.MODEL,
        "diarize": deepgram.DIARIZE,
        "running_jobs": JOBS.running(),
        "concurrency": CONCURRENCY,
        "max_duration_s": MAX_DURATION_S,
        "max_jobs_per_hour": MAX_JOBS_PER_HOUR,
        "max_meta_calls_per_hour": MAX_META_CALLS_PER_HOUR,
        "impersonation": IMPERSONATION,
        "cookies": bool(media.COOKIES_FILE and os.path.exists(media.COOKIES_FILE)),
        "music_id": _music_status(),
        "find_on_screen_matching": ({"method": MATCH_METHOD, "model": embeddings.MODEL,
                                     "min_score": EMBED_MIN_SCORE} if embeddings.configured()
                                    else {"method": MATCH_METHOD}),
    }


def _music_status() -> dict:
    from music_id import spotify
    try:
        provider = music_id.get_provider()
    except UserError as exc:
        return {"provider": music_id.PROVIDER, "configured": False, "error": str(exc)}
    used = music_cache.requests_this_month()
    return {"provider": provider.name, "configured": provider.configured(),
            "spotify_lookup": spotify.configured(),
            "requests_this_month": f"{used} of {AUDD_MAX_REQUESTS_PER_MONTH}" if AUDD_MAX_REQUESTS_PER_MONTH
            else f"{used} (no monthly limit)"}


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


# Icon for connector lists: advertised in serverInfo.icons above, and served as the site
# favicon for clients that look the domain up instead.
STATIC = Path(__file__).parent / "static"
_ICONS = {"/icon.png": "image/png", "/icon.svg": "image/svg+xml", "/favicon.ico": "image/x-icon"}


def _icon_route(path: str, media_type: str) -> None:
    @mcp.custom_route(path, methods=["GET"])
    async def icon(_: Request) -> FileResponse:
        return FileResponse(STATIC / path.lstrip("/"), media_type=media_type,
                            headers={"Cache-Control": "public, max-age=86400"})


for _path, _type in _ICONS.items():
    _icon_route(_path, _type)


@mcp.custom_route("/", methods=["GET"])
async def home(_: Request) -> HTMLResponse:
    return HTMLResponse(
        '<!doctype html><title>ReelSense</title>'
        '<link rel="icon" href="/favicon.ico"><link rel="icon" type="image/svg+xml" href="/icon.svg">'
        '<p>ReelSense: an MCP server. Add <code>/mcp</code> to this address as a connector.</p>'
    )


def build_app():
    # Plus the names a tunnel container on the same Docker network may use (README, tunnel.bat).
    hosts = ALLOWED_HOSTS + ["localhost", "127.0.0.1", "host.docker.internal",
                             "reelsense-server", "reelsense-server-auth", "reelsense"]
    return mcp.http_app(path="/mcp", stateless_http=True, allowed_hosts=hosts)


if __name__ == "__main__":
    log.info(f"transcribe-mcp on :{PORT} url={PUBLIC_URL} auth={signin.PROVIDER} "
             f"allowlist={'on' if signin.gate_on() else 'off'} model={deepgram.MODEL} "
             f"yt-dlp={yt_dlp.version.__version__}")
    uvicorn.run(build_app(), host="0.0.0.0", port=PORT, log_level="warning", proxy_headers=True,
                forwarded_allow_ips=FORWARDED_ALLOW_IPS)
