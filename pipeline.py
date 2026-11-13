"""The transcription pipeline, without the MCP server around it: probe, then the cached
transcript or download + Deepgram. Synchronous; runs on the server's worker pool."""
from typing import Optional

import deepgram
import media
import transcript_cache
from media import UserError


def _fmt_s(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60} min" if seconds >= 120 else f"{seconds} s"


def transcribe_cached(url: str, tag: str, language: Optional[str] = None,
                      start: Optional[float] = None, end: Optional[float] = None,
                      chapter: Optional[str] = None, max_duration_s: int = 7200) -> dict:
    """{**video metadata, **transcript}. A whole video's transcript is cached by video and
    language, so the same video isn't downloaded or transcribed twice; a part (start, end
    or chapter) is always transcribed fresh. Adds "cached": True on a cache hit. Raises
    UserError. Leaves temp files under tag for media.cleanup."""
    meta = media.probe(url, tag, with_key=True)
    video_key = meta.pop("video_key", None)
    duration = meta.get("duration") or 0
    section = media.resolve_section(meta, start, end, chapter)
    # The cap is on what gets transcribed, so a section of a long stream is fine.
    length = ((section["end"] or duration) - section["start"]) if section else duration
    if length > max_duration_s:
        what = "Section" if section else "Video"
        raise UserError(f"{what} is {_fmt_s(length)}, over the {_fmt_s(max_duration_s)} limit. "
                        "Pass start and end (or a chapter) to transcribe part of it.")
    cache_key = transcript_cache.key(video_key, language) if video_key and not section else None
    if cache_key and (hit := transcript_cache.get(cache_key)):
        return {**meta, **hit, "cached": True}
    path = media.download_audio(url, tag, section)
    transcript = deepgram.transcribe(path, language)
    if section:
        # Deepgram times are from the clip's start; shift them to the video's clock.
        for seg in transcript["segments"]:
            seg["start"] = round(seg["start"] + section["start"], 2)
            seg["end"] = round(seg["end"] + section["start"], 2)
        transcript["section"] = section
    elif cache_key:
        transcript_cache.put(cache_key, transcript)
    return {**meta, **transcript}
