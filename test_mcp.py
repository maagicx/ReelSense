"""End-to-end client test.

    python test_mcp.py <base-url>
    python test_mcp.py https://mcp.example.com
    python test_mcp.py https://mcp.example.com --quick     # skip transcription
    python test_mcp.py https://mcp.example.com --measure   # sizes per field only
    python test_mcp.py https://mcp.example.com --music     # identify_music only

Opens a browser for sign-in (whichever AUTH_PROVIDER the server uses) on first run. Checks: no token / bad token -> 401
with OAuth metadata, sign-in, health shows who you are, all tools listed, the channel
card resource, identify_music argument checks, video_info (slim by default, full=true, card fields in _meta.card),
list_videos, search_videos, channel_info, get_comments, get_frames,
transcribe_video on a short YouTube video and a TikTok video, then find_on_screen (one copy of the transcript,
as timestamped lines; segments only on request). --measure prints what the model reads,
field by field, for MEASURE (one of them a two-speaker interview, so it also checks the
speaker labels) and costs about 45 minutes of Deepgram. --music runs the identify_music acceptance cases
(MUSIC) and spends roughly 60 AudD requests the first time (the full scan of the mix is
most of it); a rerun is served from the cache.
"""
import asyncio
import json
import sys
import time

import requests
from fastmcp import Client
from fastmcp.client import oauth_callback
from fastmcp.client.transports import StreamableHttpTransport

TOOLS = ["channel_info", "find_on_apple_music", "find_on_screen", "find_on_spotify", "get_comments", "get_frames", "get_transcript", "health", "help", "identify_music",
         "list_videos", "search_videos", "transcribe_video", "video_info"]

VIDEOS = [
    "https://www.youtube.com/watch?v=jNQXAC9IVRw",  # "Me at the zoo", 19 s
    "https://www.tiktok.com/@scout2015/video/6718335390845095173",
]
CHANNEL = "https://www.tiktok.com/@scout2015"
YT_CHANNEL = "https://www.youtube.com/@markdarwin"  # bare: must list its Videos tab

# --measure: (label, transcribe_video arguments). The second is 40 min of a two-person interview.
MEASURE = [("test video 3:57", {"url": "https://www.youtube.com/watch?v=nknZCJDds-o"}),
           ("interview, first 40 min", {"url": "https://www.youtube.com/watch?v=UclrVWafRAI", "end": "40:00"})]

# --music: (label, identify_music arguments, check on the result).
MUSIC = [
    ("commercial track", {"url": "https://www.youtube.com/watch?v=fmI_Ndrxy14"},  # Imagine Dragons - Warriors
     lambda r: r.get("matched") and "warriors" in (r["tracks"][0].get("title") or "").lower()
     and r["requests_used"] == (0 if r.get("cached") else 1) and r["tracks"][0].get("spotify_url")),
    ("live classical clip", {"url": "https://www.tiktok.com/@reelsclassics.it/video/7699275236725771542"},
     lambda r: "Christian Li" in (r.get("post_credit") or "")),
    ("speech only", {"url": "https://www.youtube.com/watch?v=jNQXAC9IVRw"},
     lambda r: r.get("status") != "error" and "post_credit" in r),
    ("mix, full scan", {"url": "https://www.youtube.com/watch?v=c0x-C5E5zsY", "mode": "full"},  # "80s Mix", 28 min
     lambda r: len(r.get("tracks") or []) >= 2 and all("offset_seconds" in t for t in r["tracks"])),
]

# v1 fields may be null; everything added in v2 is omitted rather than null.
V1_FIELDS = {"title", "channel", "duration", "published", "platform", "url", "stats"}


def show(label: str, result) -> dict:
    data = result.data if hasattr(result, "data") and result.data is not None else result.structured_content
    if data is None and result.content and result.content[0].type == "text":
        data = json.loads(result.content[0].text)  # video tools: JSON text, no structuredContent
    if isinstance(data, dict) and isinstance(data.get("segments"), list):
        preview = {**data, "segments": data["segments"][:3] + (["…"] if len(data["segments"]) > 3 else [])}
    else:
        preview = data
    print(f"\n== {label}\n{json.dumps(preview, indent=2, ensure_ascii=False, default=str)[:2500]}")
    return data


def check_unauthenticated(base: str) -> int:
    failures = 0
    for label, headers in (("no token", {}), ("bad token", {"Authorization": "Bearer nope"})):
        r = requests.post(f"{base}/mcp", headers=headers, json={}, timeout=15)
        challenge = r.headers.get("www-authenticate", "")
        print(f"{label} -> HTTP {r.status_code} {challenge}")
        failures += r.status_code != 401 or "resource_metadata" not in challenge
    for path in ("/.well-known/oauth-protected-resource/mcp", "/.well-known/oauth-authorization-server"):
        r = requests.get(f"{base}{path}", timeout=15)
        print(f"{path} -> HTTP {r.status_code}")
        failures += r.status_code != 200
    return failures


async def main(base: str, quick: bool) -> int:
    base = base.rstrip("/")
    failures = check_unauthenticated(base)

    bearer = Client(StreamableHttpTransport(f"{base}/mcp", auth="oauth"), timeout=900)
    async with bearer:
        listed_tools = await bearer.list_tools()
        tools = sorted(t.name for t in listed_tools)
        print(f"\ntools: {tools}")
        failures += tools != TOOLS
        # The video and channel tools carry an MCP App card; the ui:// resource must be readable.
        card_uris = {t.name: (t.meta or {}).get("ui", {}).get("resourceUri") for t in listed_tools}
        card_uri = card_uris["channel_info"]
        card = (await bearer.read_resource(card_uri))[0]
        print(f"card: {card_uri} {card.mimeType} {len(card.text)} chars")
        failures += card.mimeType != "text/html;profile=mcp-app" or "ui/initialize" not in card.text
        failures += not card_uri or any(card_uris[t] != card_uri for t in
                                        ("video_info", "transcribe_video", "get_transcript", "identify_music", "find_on_spotify",
                                         "find_on_apple_music", "find_on_screen", "get_frames"))
        me = show("health", await bearer.call_tool("health", {}))
        failures += me.get("user") in (None, "?") or me.get("impersonation") is not True
        guide = show("help", await bearer.call_tool("help", {}))
        failures += "Transcribe" not in (guide.get("guide") or "") or "used_this_hour" not in guide
        failures += "spotify_searches" not in (guide.get("used_this_hour") or {})
        badtime = show("transcribe_video bad time", await bearer.call_tool(
            "transcribe_video", {"url": VIDEOS[0], "start": "soon"}))
        failures += badtime.get("status") != "error"
        badmode = show("identify_music bad mode", await bearer.call_tool(
            "identify_music", {"url": VIDEOS[0], "mode": "slow"}))
        failures += badmode.get("status") != "error"
        failures += "music_id" not in me
        noargs = show("find_on_spotify no args", await bearer.call_tool("find_on_spotify", {}))
        failures += noargs.get("status") != "error"
        if (me.get("music_id") or {}).get("spotify_lookup"):
            raw = await bearer.call_tool("find_on_spotify", {"artist": "Imagine Dragons", "title": "Warriors"})
            sp = show("find_on_spotify Warriors", raw)
            failures += not any("open.spotify.com/track/" in (t.get("spotify_url") or "") for t in sp.get("tracks") or [])
            failures += ((raw.meta or {}).get("music") or {}).get("mode") != "search"
        raw = await bearer.call_tool("find_on_apple_music", {"artist": "Imagine Dragons", "title": "Warriors"})
        am = show("find_on_apple_music Warriors", raw)
        failures += not any("music.apple.com/" in (t.get("apple_music_url") or "") for t in am.get("tracks") or [])
        bogus = show("get_transcript unknown", await bearer.call_tool("get_transcript", {"job_id": "nope"}))
        failures += bogus.get("status") != "error"
        for url in VIDEOS:
            raw = await bearer.call_tool("video_info", {"url": url})
            info = show(f"video_info {url}", raw)
            card = (raw.meta or {}).get("card") or {}
            # The model's copy is slim and single; the card gets the thumbnail in _meta.
            failures += info.get("status") == "error" or not card.get("thumbnail") or "thumbnail" in info
            failures += raw.structured_content is not None
            failures += any(v is None for k, v in info.items() if k not in V1_FIELDS)
        full = show("video_info full", await bearer.call_tool("video_info", {"url": VIDEOS[0], "full": True}))
        failures += not full.get("thumbnail")
        show("video_info bad url", await bearer.call_tool("video_info", {"url": "https://example.com/nope"}))
        listed = show("list_videos", await bearer.call_tool("list_videos", {"url": CHANNEL, "limit": 5}))
        failures += len(listed.get("videos") or []) != 5
        yt = show("list_videos youtube", await bearer.call_tool("list_videos", {"url": YT_CHANNEL, "limit": 5}))
        failures += len(yt.get("videos") or []) != 5 or "published_note" not in yt
        top = show("list_videos popular", await bearer.call_tool(
            "list_videos", {"url": YT_CHANNEL, "limit": 5, "sort": "popular"}))
        views = [v.get("views") or 0 for v in top.get("videos") or []]
        failures += len(views) != 5 or views != sorted(views, reverse=True) or top.get("scanned", 0) <= 5
        ch = show("channel_info youtube", await bearer.call_tool("channel_info", {"url": YT_CHANNEL}))
        failures += not isinstance(ch.get("subscribers"), int) or not ch.get("handle") or "note" in ch
        owner = show("channel_info from a video link", await bearer.call_tool("channel_info", {"url": VIDEOS[0]}))
        failures += owner.get("status") == "error" or not owner.get("name")
        tt = await bearer.call_tool("channel_info", {"url": CHANNEL})
        ch = show("channel_info tiktok", tt)
        failures += ch.get("status") == "error" or not ch.get("name") or "note" not in ch
        # TikTok's avatar link is signed, so the card gets the image inline in _meta.
        image = (tt.meta or {}).get("image") or ""
        print(f"channel_info tiktok _meta.image: {image[:30]}… {len(image)} chars")
        failures += not ch.get("avatar") or not image.startswith("data:image/")
        found = show("search_videos", await bearer.call_tool("search_videos", {"query": "me at the zoo", "limit": 3}))
        failures += len(found.get("videos") or []) != 3
        recent = show("search_videos month", await bearer.call_tool(
            "search_videos", {"query": "yt-dlp", "limit": 5, "uploaded_within": "month"}))
        dates = [v.get("published") for v in recent.get("videos") or []]
        failures += not dates or None in dates or dates != sorted(dates, reverse=True)
        bad = show("search_videos bad window", await bearer.call_tool(
            "search_videos", {"query": "yt-dlp", "uploaded_within": "decade"}))
        failures += bad.get("status") != "error"
        other = show("search_videos tiktok",
                     await bearer.call_tool("search_videos", {"query": "cats", "platform": "tiktok"}))
        failures += other.get("status") != "error"
        for url in VIDEOS:  # YouTube seeks into HLS; TikTok goes through yt-dlp's own download
            shot = await bearer.call_tool_mcp("get_frames", {"url": url, "timestamps": [2, "0:05"]})
            images = [b for b in shot.content if b.type == "image"]
            print(f"\n== get_frames {url}: {len(images)} images, error={shot.isError}")
            failures += shot.isError or len(images) != 2
        late = await bearer.call_tool_mcp("get_frames", {"url": VIDEOS[0], "timestamps": ["9:00"]})
        failures += not late.isError
        said = show("get_comments", await bearer.call_tool("get_comments", {"url": VIDEOS[0], "limit": 10}))
        failures += not 0 < len(said.get("comments") or []) <= 10
        vimeo = show("get_comments vimeo",
                     await bearer.call_tool("get_comments", {"url": "https://vimeo.com/76979871"}))
        failures += vimeo.get("status") != "error"
        if quick:
            return failures
        part = show("transcribe_video section 0:05-0:12", await bearer.call_tool(
            "transcribe_video", {"url": VIDEOS[0], "start": "0:05", "end": 12, "segments": True}))
        segs = part.get("segments") or []
        failures += part.get("section", {}).get("start") != 5 or not segs or segs[0]["start"] < 5
        for url in VIDEOS:
            t0 = time.time()
            data = show(f"transcribe_video {url}",
                        await bearer.call_tool("transcribe_video", {"url": url}))
            while data.get("status") == "running":
                await asyncio.sleep(15)
                data = show("get_transcript", await bearer.call_tool("get_transcript", {"job_id": data["job_id"]}))
            print(f"-- {time.time() - t0:.1f}s")
            # An empty transcript with a note is a pass: the video may simply have no speech.
            failures += data.get("status") == "error" or not (data.get("transcript") or data.get("note"))
            # One copy of the transcript: timestamped lines, no segment array unless asked.
            failures += "segments" in data or not (data.get("note") or data["transcript"].startswith("[0:"))
        # find_on_screen: "Me at the zoo" talks about elephants. Transcribed above, so this
        # reuses the cached transcript; a word never said gives labelled fallback frames.
        for query, want_fallback in (("elephants", False), ("submarine", True)):
            r = await bearer.call_tool_mcp("find_on_screen", {"url": VIDEOS[0], "query": query, "max_frames": 3})
            head = json.loads(r.content[0].text) if not r.isError else {}
            images = [b for b in r.content if b.type == "image"]
            print(f"\n== find_on_screen {query!r}: fallback={head.get('fallback')} "
                  f"matches={head.get('matches')} {len(images)} images error={r.isError}")
            failures += r.isError or head.get("fallback") is not want_fallback or not images
            failures += not want_fallback and not head.get("matches")
            failures += "frames" not in (r.meta or {})
    return failures


def _sizes(text: str) -> str:
    view = json.loads(text)
    fields = sorted(view.items(), key=lambda kv: -len(json.dumps(kv[1], ensure_ascii=False)))
    return ", ".join(f"{k} {len(json.dumps(v, ensure_ascii=False))}" for k, v in fields)


async def measure(base: str) -> int:
    """Characters the model reads per tool and field, for comparing before/after."""
    failures = 0
    bearer = Client(StreamableHttpTransport(f"{base.rstrip('/')}/mcp", auth="oauth"), timeout=900)
    async with bearer:
        for label, args in MEASURE:
            for tool, extra in (("video_info", {}), ("video_info", {"full": True}), ("transcribe_video", {})):
                call = {"url": args["url"], **extra} if tool == "video_info" else {**args, **extra}
                r = await bearer.call_tool_mcp(tool, call)
                text = "".join(b.text for b in r.content if b.type == "text")
                sc = len(json.dumps(r.structuredContent)) if r.structuredContent else 0
                while tool == "transcribe_video" and json.loads(text).get("status") == "running":
                    await asyncio.sleep(15)
                    r = await bearer.call_tool_mcp("get_transcript", {"job_id": json.loads(text)["job_id"]})
                    text = "".join(b.text for b in r.content if b.type == "text")
                name = tool + (" full" if extra else "")
                print(f"\n{label} | {name}: {len(text)} chars to the model, structuredContent {sc}")
                print("   " + _sizes(text))
                if tool == "transcribe_video":
                    view = json.loads(text)
                    print("   transcript starts:", repr(view.get("transcript", "")[:160]))
                    if "interview" in label:
                        print(f"   speakers: {view.get('speakers')}")
                        failures += not view.get("speakers") or " S1: " not in view.get("transcript", "")
    return failures


async def music(base: str) -> int:
    """identify_music acceptance cases, each run twice: the second must come from the cache."""
    failures = 0
    bearer = Client(StreamableHttpTransport(f"{base.rstrip('/')}/mcp", auth="oauth"), timeout=900)
    async with bearer:
        for label, args, ok in MUSIC:
            raw = await bearer.call_tool("identify_music", args)
            first = show(f"identify_music {label}", raw)
            # The card's copy (with album art) is in _meta.music; the model's copy has no art.
            failures += first.get("status") != "error" and (
                "music" not in (raw.meta or {}) or any("artwork" in t for t in first.get("tracks") or []))
            passed = bool(first.get("status") != "error" and ok(first))
            again = show(f"identify_music {label} again", await bearer.call_tool("identify_music", args))
            cached = again.get("cached") is True and again.get("requests_used") == 0
            print(f"-- {label}: {'pass' if passed else 'FAIL'}, cache {'hit' if cached else 'MISS'}")
            failures += (not passed) + (not cached)
    return failures


def brand_signin_page(base: str) -> None:
    """Show the ReelSense icon, not FastMCP's, on the local "Authentication successful" page."""
    logo, page = oauth_callback.create_logo, oauth_callback.create_callback_html
    oauth_callback.create_logo = lambda *a, **k: logo(f"{base.rstrip('/')}/icon.svg", "ReelSense")
    oauth_callback.create_callback_html = lambda *a, **k: page(*a, **{**k, "title": "ReelSense"})


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 1:
        sys.exit(__doc__)
    brand_signin_page(args[0])
    if "--measure" in sys.argv:
        failed = asyncio.run(measure(args[0]))
    elif "--music" in sys.argv:
        failed = asyncio.run(music(args[0]))
    else:
        failed = asyncio.run(main(args[0], "--quick" in sys.argv))
    print(f"\n{'OK' if not failed else f'{failed} FAILURE(S)'}")
    sys.exit(1 if failed else 0)
