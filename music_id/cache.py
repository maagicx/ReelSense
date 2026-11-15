"""identify_music results by video and mode, in SQLite on the /data volume, so the same
video gives the same answer without paying for another recognition, and the server's
running count of AudD requests per calendar month (UTC) for AUDD_MAX_REQUESTS_PER_MONTH.
Best effort: a broken cache never fails a call."""
import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Iterator, Optional

PATH = os.environ.get("MUSIC_CACHE_PATH") or os.path.join(
    os.environ.get("FASTMCP_HOME") or os.environ.get("TMP_DIR", "/tmp"), "music_cache.sqlite")

log = logging.getLogger("transcribe-mcp")
_lock = threading.Lock()


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    """A connection that commits (or rolls back) and is always closed: a with-block on
    the connection alone only ends the transaction, and an open handle locks the file on
    Windows."""
    con = sqlite3.connect(PATH, timeout=10)
    try:
        with con:
            con.execute("CREATE TABLE IF NOT EXISTS music (key TEXT PRIMARY KEY, value TEXT, expires REAL)")
            con.execute("CREATE TABLE IF NOT EXISTS usage (month TEXT PRIMARY KEY, requests INTEGER)")
            yield con
    finally:
        con.close()


def _month() -> str:
    return time.strftime("%Y-%m", time.gmtime())


def requests_this_month() -> int:
    try:
        with _lock, _db() as con:
            row = con.execute("SELECT requests FROM usage WHERE month = ?", (_month(),)).fetchone()
        return row[0] if row else 0
    except sqlite3.Error as exc:
        log.warning(f"[music] usage read failed: {exc}")
        return 0


def add_requests(n: int) -> None:
    if n <= 0:
        return
    try:
        with _lock, _db() as con:
            con.execute("INSERT INTO usage VALUES (?, ?) ON CONFLICT(month) DO UPDATE SET "
                        "requests = requests + excluded.requests", (_month(), n))
    except sqlite3.Error as exc:
        log.warning(f"[music] usage write failed: {exc}")


def get(key: str) -> Optional[dict]:
    try:
        with _lock, _db() as con:
            row = con.execute("SELECT value, expires FROM music WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row and row[1] > time.time() else None
    except (sqlite3.Error, OSError, ValueError) as exc:
        log.warning(f"[music] cache read failed: {exc}")
        return None


def put(key: str, value: dict, ttl_s: float) -> None:
    try:
        with _lock, _db() as con:
            con.execute("INSERT OR REPLACE INTO music VALUES (?, ?, ?)",
                        (key, json.dumps(value, ensure_ascii=False), time.time() + ttl_s))
            con.execute("DELETE FROM music WHERE expires < ?", (time.time(),))
    except (sqlite3.Error, OSError) as exc:
        log.warning(f"[music] cache write failed: {exc}")
