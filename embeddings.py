"""Text embeddings from Voyage AI, for find_on_screen's meaning-based matching.

Transcript lines are embedded once and kept in the transcript cache's SQLite file (keyed by
the model and a hash of the lines, so the same transcript never pays twice); each query is
embedded fresh. Vectors are stored as float32 and normalised, so a dot product is the
cosine similarity. Without VOYAGE_API_KEY nothing here is called and find_on_screen matches
by keyword only. Best effort: any failure raises EmbeddingError and the caller falls back."""
import array
import hashlib
import logging
import math
import os
import sqlite3
import threading
import time
from typing import Optional

import requests

import transcript_cache

API_KEY = os.environ.get("VOYAGE_API_KEY", "").strip()
MODEL = (os.environ.get("VOYAGE_MODEL") or "voyage-4-lite").strip()
DIMENSION = int(os.environ.get("VOYAGE_DIMENSION") or 512)
URL = "https://api.voyageai.com/v1/embeddings"
BATCH = 500  # lines per request: under Voyage's 1,000-input cap with room for long lines
TIMEOUT_S = 30
TTL_S = transcript_cache.TTL_S  # expire with the transcript they belong to

log = logging.getLogger("transcribe-mcp")
_lock = threading.Lock()


class EmbeddingError(Exception):
    pass


def configured() -> bool:
    return bool(API_KEY)


def _normalise(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def _request(texts: list[str], input_type: str) -> tuple[list[list[float]], int]:
    """(vectors in input order, tokens billed)."""
    try:
        resp = requests.post(URL, timeout=TIMEOUT_S, headers={"Authorization": f"Bearer {API_KEY}"},
                             json={"input": texts, "model": MODEL, "input_type": input_type,
                                   "output_dimension": DIMENSION})
    except requests.RequestException as exc:
        raise EmbeddingError(f"Voyage unreachable: {exc}") from exc
    if resp.status_code != 200:
        raise EmbeddingError(f"Voyage HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        body = resp.json()
        rows = sorted(body["data"], key=lambda r: r["index"])
        vectors = [_normalise(r["embedding"]) for r in rows]
    except (ValueError, KeyError, TypeError) as exc:
        raise EmbeddingError(f"Voyage returned an unexpected response: {exc}") from exc
    if len(vectors) != len(texts):
        raise EmbeddingError(f"Voyage returned {len(vectors)} vectors for {len(texts)} inputs")
    return vectors, int((body.get("usage") or {}).get("total_tokens") or 0)


def embed_query(text: str) -> list[float]:
    return _request([text], "query")[0][0]


# ---- cached document embeddings

def _key(texts: list[str]) -> str:
    digest = hashlib.sha256("\n".join(texts).encode("utf-8")).hexdigest()[:32]
    return f"{MODEL}:{DIMENSION}:{digest}"


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(transcript_cache.PATH, timeout=10)
    con.execute("CREATE TABLE IF NOT EXISTS embeddings (key TEXT PRIMARY KEY, n INTEGER, dim INTEGER, "
                "vectors BLOB, expires REAL)")
    return con


def _load(key: str, n: int) -> Optional[list[list[float]]]:
    try:
        with _lock, _db() as con:
            row = con.execute("SELECT n, dim, vectors, expires FROM embeddings WHERE key = ?", (key,)).fetchone()
    except sqlite3.Error as exc:
        log.warning(f"[embed] cache read failed: {exc}")
        return None
    if not row or row[3] <= time.time() or row[0] != n:
        return None
    flat = array.array("f")
    flat.frombytes(row[2])
    dim = row[1]
    return [list(flat[i * dim:(i + 1) * dim]) for i in range(n)]


def _store(key: str, vectors: list[list[float]]) -> None:
    flat = array.array("f", (x for v in vectors for x in v))
    try:
        with _lock, _db() as con:
            con.execute("INSERT OR REPLACE INTO embeddings VALUES (?, ?, ?, ?, ?)",
                        (key, len(vectors), len(vectors[0]) if vectors else 0, flat.tobytes(),
                         time.time() + TTL_S))
            con.execute("DELETE FROM embeddings WHERE expires < ?", (time.time(),))
    except sqlite3.Error as exc:
        log.warning(f"[embed] cache write failed: {exc}")


def embed_lines(texts: list[str]) -> list[list[float]]:
    """One vector per line, from the cache when this exact list was embedded before."""
    if not texts:
        return []
    key = _key(texts)
    if (cached := _load(key, len(texts))) is not None:
        return cached
    vectors, tokens = [], 0
    for i in range(0, len(texts), BATCH):
        part, used = _request([t or " " for t in texts[i:i + BATCH]], "document")
        vectors += part
        tokens += used
    _store(key, vectors)
    log.info(f"[embed] {len(texts)} lines {tokens} tokens {MODEL}")
    return vectors
