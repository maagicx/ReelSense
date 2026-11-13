"""find_on_screen's logic: which transcript lines a query is about, merged into moments,
and when in each moment to take a frame. No I/O of its own: the embedding matcher is
handed the functions that fetch embeddings.

Matching sits behind Matcher: KeywordMatcher (words, stemmed) and HybridMatcher (meaning
by embeddings, plus the keyword score, falling back to keywords alone when embeddings
fail). find() takes the matcher to use; MATCHER is the default."""
import logging
import re
from typing import Callable, Optional, Protocol

log = logging.getLogger("transcribe-mcp")

MERGE_GAP_S = 3.0  # matching lines this close together are one moment
END_MARGIN_S = 0.5  # media.frames refuses a time at or past the end
MIN_FRAME_GAP_S = 0.5  # frames closer than this would be the same picture


class Matcher(Protocol):
    def match(self, query: str, segments: list[dict]) -> list[tuple[int, float]]:
        """(index into segments, relevance > 0) for each segment about the query."""
        ...


STOPWORDS = frozenset("""
a an and are as at be been but by can could did do does for from had has have he her his
how i if in into is it its me my of on or our she so that the their them then there these
they this those to was we were what when where which who why will with you your about
show shows showing see talk talks talking mention mentions mentioned say says said""".split())

_WORD = re.compile(r"[^\W_]+(?:'[^\W_]+)?")
_SUFFIXES = ("ations", "ation", "ments", "ment", "ness", "ings", "ing", "ies", "ers", "er",
             "ed", "es", "ly", "s")


def stem(word: str) -> str:
    """A small suffix stripper: trading, traded, trades -> trad; accounts -> account.
    Crude, but the same on both sides of the comparison, which is what matters."""
    word = word.lower().replace("'", "")
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            word = word[: -len(suffix)]
            if suffix == "ies":
                word += "y"
            break
    if word.endswith("e") and len(word) > 3:  # trade / trad(ing) meet at "trad"
        word = word[:-1]
    if len(word) > 3 and word[-1] == word[-2] and word[-1] not in "aeiouls":  # stopp(ed)
        word = word[:-1]
    return word


def terms(text: str) -> list[str]:
    """Stemmed content words of text, in order (stopwords dropped)."""
    return [stem(w) for w in _WORD.findall(text.lower()) if w not in STOPWORDS]


class KeywordMatcher:
    """Case-insensitive, stemmed keyword overlap. Score: share of the query's distinct
    terms that the segment contains, plus 1 when the query's terms appear in it in order
    (the phrase itself)."""

    def match(self, query: str, segments: list[dict]) -> list[tuple[int, float]]:
        want = list(dict.fromkeys(terms(query)))
        if not want:
            return []
        phrase = " ".join(terms(query))
        hits = []
        for i, seg in enumerate(segments):
            have = terms(seg.get("text") or "")
            found = set(want) & set(have)
            if not found:
                continue
            score = len(found) / len(want)
            if len(want) > 1 and f" {phrase} " in f" {' '.join(have)} ":
                score += 1.0
            hits.append((i, round(score, 3)))
        return hits


class HybridMatcher:
    """Meaning first: cosine similarity between the query's embedding and each line's,
    so "trunks" can find "long pumps" (a mishearing) and "pricing" finds "£49 a month".
    A line counts if its similarity is at least min_score and within margin of the best
    line's, or if it matches by keyword. Score = similarity + keyword score, so a line
    that says the words outranks one that only means them. Any embedding failure falls
    back to KeywordMatcher, logged."""

    def __init__(self, embed_query: Callable[[str], list[float]],
                 embed_lines: Callable[[list[str]], list[list[float]]],
                 min_score: float = 0.45, margin: float = 0.15,
                 keyword: Optional[Matcher] = None) -> None:
        self.embed_query, self.embed_lines = embed_query, embed_lines
        self.min_score, self.margin = min_score, margin
        self.keyword = keyword or KeywordMatcher()

    def match(self, query: str, segments: list[dict]) -> list[tuple[int, float]]:
        by_keyword = dict(self.keyword.match(query, segments))
        if not segments:
            return []
        try:
            q = self.embed_query(query)
            lines = self.embed_lines([(s.get("text") or "").strip() for s in segments])
        except Exception as exc:  # any failure: keywords alone, the tool still answers
            log.warning(f"[embed] falling back to keywords: {exc}")
            return list(by_keyword.items())
        sims = [sum(a * b for a, b in zip(q, v)) for v in lines]
        best = max(sims)
        floor = max(self.min_score, best - self.margin)
        log.info(f"[embed] query {query!r}: best similarity {best:.3f}, floor {floor:.3f}")
        hits = []
        for i, sim in enumerate(sims):
            if sim >= floor or i in by_keyword:
                hits.append((i, round(sim + by_keyword.get(i, 0.0), 3)))
        return hits


MATCHER: Matcher = KeywordMatcher()


def merge(segments: list[dict], hits: list[tuple[int, float]], gap: float = MERGE_GAP_S) -> list[dict]:
    """Matching segments as moments: neighbours within gap seconds are joined into one
    {start, end, text, score, speaker?}; speaker only if one speaker throughout."""
    spans: list[dict] = []
    for i, score in sorted(hits, key=lambda h: segments[h[0]]["start"]):
        seg = segments[i]
        speaker = seg.get("speaker")
        if spans and seg["start"] - spans[-1]["end"] <= gap:
            last = spans[-1]
            last["end"] = max(last["end"], seg["end"])
            last["text"] = f"{last['text']} {seg['text'].strip()}"
            last["score"] = max(last["score"], score)
            last["speakers"].add(speaker)
            continue
        spans.append({"start": seg["start"], "end": seg["end"], "text": seg["text"].strip(),
                      "score": score, "speakers": {speaker}})
    for span in spans:
        speakers = span.pop("speakers")
        if len(speakers) == 1 and None not in speakers:
            span["speaker"] = speakers.pop()
    return spans


def select(spans: list[dict], max_frames: int) -> list[dict]:
    """The max_frames most relevant moments (ties to the earlier one), in time order."""
    best = sorted(spans, key=lambda s: (-s["score"], s["start"]))[:max_frames]
    return sorted(best, key=lambda s: s["start"])


def frame_time(span: dict, offset: float, duration: Optional[float]) -> float:
    """start + offset, but no later than the moment's end or just before the video's end,
    and never before the start (or 0)."""
    t, earliest = min(span["start"] + offset, span["end"]), span["start"]
    if duration:
        last = max(duration - END_MARGIN_S, 0.0)
        t, earliest = min(t, last), min(earliest, last)
    return round(max(t, earliest, 0.0), 2)


def fallback_times(duration: Optional[float], n: int) -> list[float]:
    """n evenly spaced times: the midpoints of n equal parts of the video."""
    if not duration or duration <= 0:
        return [0.0]
    n = max(1, min(n, int(duration / MIN_FRAME_GAP_S) or 1))
    return [round(duration * (i + 0.5) / n, 2) for i in range(n)]


def find(query: str, segments: list[dict], duration: Optional[float], max_frames: int,
         offset: float, matcher: Optional[Matcher] = None) -> tuple[list[dict], bool]:
    """(matches, fallback). matches are [{start, end, text, speaker?, frame_time}] in time
    order, one frame each. With nothing matching, fallback is True and matches is empty:
    take fallback_times instead."""
    hits = (matcher or MATCHER).match(query, segments)
    if not hits:
        return [], True
    matches, taken = [], []
    for span in select(merge(segments, hits), max_frames):
        t = frame_time(span, offset, duration)
        if any(abs(t - u) < MIN_FRAME_GAP_S for u in taken):
            continue  # two moments clamped onto the same picture
        taken.append(t)
        match = {"start": span["start"], "end": span["end"], "text": span["text"]}
        if "speaker" in span:
            match["speaker"] = span["speaker"]
        match["frame_time"] = t
        matches.append(match)
    return matches, False
