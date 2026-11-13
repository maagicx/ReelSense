"""Whole-video transcripts by video and language, in SQLite on the /data volume, so asking
about a video again (find_on_screen after transcribe_video, or a retry) doesn't download
and transcribe it a second time. Only the transcript is kept, not the metadata, which is
probed fresh each call. Best effort: a broken cache never fails a call."""
import json
import logging
import os
import sqlite3
import threading
import time
from typing import Optional

PATH = os.environ.get("TRANSCRIPT_CACHE_PATH") or os.path.join(
    os.environ.get("FASTMCP_HOME") or os.environ.get("TMP_DIR", "/tmp"), "transcript_cache.sqlite")
TTL_S = int(os.environ.get("TRANSCRIPT_CACHE_TTL_S") or 7 * 86400)
# What deepgram.transcribe returns, without anything about the video itself.
FIELDS = ("transcript", "segments", "language", "audio_s", "speakers", "note")

log = logging.getLogger("transcribe-mcp")
_lock = threading.Lock()


def key(video_key: str, language: Optional[str]) -> str:
    return f"{video_key}:{(language or 'auto').strip().lower()}"


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(PATH, timeout=10)
    con.execute("CREATE TABLE IF NOT EXISTS transcripts (key TEXT PRIMARY KEY, value TEXT, expires REAL)")
    return con


def get(k: str) -> Optional[dict]:
    try:
        with _lock, _db() as con:
            row = con.execute("SELECT value, expires FROM transcripts WHERE key = ?", (k,)).fetchone()
        return json.loads(row[0]) if row and row[1] > time.time() else None
    except (sqlite3.Error, OSError, ValueError) as exc:
        log.warning(f"[transcript] cache read failed: {exc}")
        return None


def put(k: str, transcript: dict, ttl_s: float = TTL_S) -> None:
    value = {f: transcript[f] for f in FIELDS if f in transcript}
    try:
        with _lock, _db() as con:
            con.execute("INSERT OR REPLACE INTO transcripts VALUES (?, ?, ?)",
                        (k, json.dumps(value, ensure_ascii=False), time.time() + ttl_s))
            con.execute("DELETE FROM transcripts WHERE expires < ?", (time.time(),))
    except (sqlite3.Error, OSError) as exc:
        log.warning(f"[transcript] cache write failed: {exc}")
