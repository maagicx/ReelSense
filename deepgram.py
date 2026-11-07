"""Deepgram pre-recorded transcription: one REST call, retried, shaped into segments."""
import os
import time
from typing import Optional

import requests

from media import UserError

API_URL = "https://api.deepgram.com/v1/listen"
API_KEY = os.environ.get("DEEPGRAM_API_KEY", "")
MODEL = os.environ.get("DEEPGRAM_MODEL", "nova-3")
DIARIZE = os.environ.get("DEEPGRAM_DIARIZE", "1").strip().lower() not in ("0", "false", "no", "off")
ATTEMPTS = 4
TIMEOUT_S = 600


def transcribe(path: str, language: Optional[str] = None) -> dict:
    """Returns {transcript, segments[{start,end,text,speaker?}], language, audio_s, speakers?}."""
    params = {"model": MODEL, "smart_format": "true", "utterances": "true", "punctuate": "true"}
    if DIARIZE:
        params["diarize"] = "true"
    if language:
        params["language"] = language
    else:
        params["detect_language"] = "true"

    headers = {"Authorization": f"Token {API_KEY}", "Content-Type": "audio/mpeg"}
    resp = None
    for attempt in range(ATTEMPTS):
        try:
            with open(path, "rb") as f:
                resp = requests.post(API_URL, params=params, headers=headers, data=f, timeout=TIMEOUT_S)
        except requests.RequestException:
            resp = None
        if resp is not None and resp.status_code < 500 and resp.status_code != 429:
            break
        if attempt < ATTEMPTS - 1:
            time.sleep(2 ** (attempt + 1))

    if resp is None:
        raise UserError("Transcription service unreachable. Try again shortly.")
    if resp.status_code == 400 and language:
        raise UserError(f"Deepgram rejected language '{language}' for model {MODEL}.")
    if resp.status_code in (401, 403):
        raise UserError("Transcription service rejected the server's API key.")
    if resp.status_code == 402:
        raise UserError("Transcription service account is out of credit.")
    if not resp.ok:
        raise UserError(f"Transcription failed (Deepgram HTTP {resp.status_code}).")

    return shape(resp.json())


_SENTENCE_END = (".", "?", "!", "…", "。", "？", "！")
MAX_MERGED_S = 30  # unpunctuated speech would otherwise merge into one huge segment


def _segment(start, end, text: str, speaker=None) -> dict:
    seg = {"start": round(start, 2), "end": round(end, 2), "text": text.strip()}
    if speaker is not None:
        seg["speaker"] = speaker
    return seg


def _paragraphs(alt: dict) -> list[dict]:
    return [p for p in (alt.get("paragraphs") or {}).get("paragraphs") or [] if p.get("sentences")]


def _sentences(alt: dict) -> list[dict]:
    """smart_format's paragraph sentences, each with its own timing. With diarize on,
    a paragraph is one speaker's turn and carries that speaker's number."""
    return [
        _segment(s["start"], s["end"], s["text"], p.get("speaker"))
        for p in _paragraphs(alt)
        for s in p["sentences"]
        if (s.get("text") or "").strip() and s.get("start") is not None and s.get("end") is not None
    ]


def _merged_utterances(utterances: list) -> list[dict]:
    """Utterances split on short pauses, often mid-sentence: join them until one ends a
    sentence or the speaker changes. Fallback for languages smart_format doesn't paragraph."""
    out, buf = [], None
    for u in utterances:
        text = (u.get("transcript") or "").strip()
        if not text:
            continue
        if buf is not None and buf.get("speaker") != u.get("speaker"):
            out.append(buf)
            buf = None
        if buf is None:
            buf = _segment(u["start"], u["end"], text, u.get("speaker"))
        else:
            buf["end"], buf["text"] = round(u["end"], 2), f"{buf['text']} {text}"
        if text.endswith(_SENTENCE_END) or buf["end"] - buf["start"] >= MAX_MERGED_S:
            out.append(buf)
            buf = None
    return out + ([buf] if buf else [])


def _label_speakers(segments: list[dict]) -> int:
    """Deepgram numbers speakers from 0; show them from 1, or drop the field when only one
    spoke so a solo video reads as before. Returns the speaker count (0 if not diarised)."""
    found = {s["speaker"] for s in segments if "speaker" in s}
    for s in segments:
        if len(found) > 1:
            s["speaker"] += 1
        else:
            s.pop("speaker", None)
    return len(found)


def _speaker_transcript(alt: dict) -> str:
    """Readable text with a "Speaker N:" line wherever the speaker changes."""
    blocks, last = [], None
    for p in _paragraphs(alt):
        text = " ".join(s["text"].strip() for s in p["sentences"] if (s.get("text") or "").strip())
        if not text:
            continue
        if p.get("speaker") != last:
            blocks.append(f"Speaker {p.get('speaker', 0) + 1}: {text}")
            last = p.get("speaker")
        else:
            blocks.append(text)
    return "\n\n".join(blocks)


def shape(body: dict) -> dict:
    results = body.get("results") or {}
    channel = (results.get("channels") or [{}])[0]
    alt = (channel.get("alternatives") or [{}])[0]
    # One segment per sentence: utterances alone split on every short pause ("safe,").
    segments = _sentences(alt) or _merged_utterances(results.get("utterances") or [])
    speakers = _label_speakers(segments)
    # smart_format paragraphs give the readable text, with paragraph breaks. With diarize
    # on Deepgram's own paragraph text says "Speaker 0:" even for one voice, so several
    # speakers get it rebuilt 1-based and a single one gets plain paragraphs.
    if speakers > 1:
        paragraphs = _speaker_transcript(alt)
    else:
        paragraphs = "\n\n".join(
            " ".join(s["text"].strip() for s in p["sentences"] if (s.get("text") or "").strip())
            for p in _paragraphs(alt))
    transcript = (
        paragraphs.strip()
        or (alt.get("transcript") or "").strip()
        or " ".join(s["text"] for s in segments)
    )
    result = {
        "transcript": transcript,
        "segments": segments,
        "language": channel.get("detected_language") if transcript else None,
        "audio_s": round((body.get("metadata") or {}).get("duration") or 0, 1),
    }
    if speakers > 1:
        result["speakers"] = speakers
    if not transcript:
        result["note"] = ("No speech detected (music, sound effects or silence). If the video "
                          "shows text or slides, get_frames can capture them.")
    return result
