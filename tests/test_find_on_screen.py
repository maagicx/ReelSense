"""find_on_screen's matching and the transcript cache. No network, no server:

    .venv/bin/python -m unittest discover tests
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import embeddings  # noqa: E402
import on_screen  # noqa: E402
import pipeline  # noqa: E402
import transcript_cache  # noqa: E402


def seg(start, end, text, speaker=None):
    s = {"start": start, "end": end, "text": text}
    if speaker is not None:
        s["speaker"] = speaker
    return s


SEGMENTS = [
    seg(0.0, 4.0, "Welcome back to the channel."),
    seg(10.0, 14.0, "Today I paper-traded with two new accounts."),
    seg(30.0, 33.0, "Let's look at the weather instead."),
    seg(50.0, 52.0, "My trading account is up this week."),
]


class KeywordMatch(unittest.TestCase):
    def test_match_found_with_stemming(self):
        matches, fallback = on_screen.find("paper trading account", SEGMENTS, 60, 5, 1.0)
        self.assertFalse(fallback)
        texts = [m["text"] for m in matches]
        self.assertIn("Today I paper-traded with two new accounts.", texts)
        self.assertIn("My trading account is up this week.", texts)
        self.assertNotIn("Let's look at the weather instead.", texts)
        # The line with all three terms ranks first, so it survives max_frames=1.
        top, _ = on_screen.find("paper trading account", SEGMENTS, 60, 1, 1.0)
        self.assertEqual(top[0]["start"], 10.0)
        self.assertEqual(top[0]["frame_time"], 11.0)

    def test_case_insensitive(self):
        matches, fallback = on_screen.find("WEATHER", SEGMENTS, 60, 5, 1.0)
        self.assertFalse(fallback)
        self.assertEqual([m["start"] for m in matches], [30.0])

    def test_results_in_time_order(self):
        matches, _ = on_screen.find("account", SEGMENTS, 60, 5, 1.0)
        self.assertEqual([m["start"] for m in matches], sorted(m["start"] for m in matches))


class NoMatch(unittest.TestCase):
    def test_fallback(self):
        matches, fallback = on_screen.find("cryptocurrency exchange", SEGMENTS, 60, 5, 1.0)
        self.assertTrue(fallback)
        self.assertEqual(matches, [])
        times = on_screen.fallback_times(60, 5)
        self.assertEqual(times, [6.0, 18.0, 30.0, 42.0, 54.0])
        self.assertTrue(all(0 <= t < 60 for t in times))

    def test_stopword_only_query_falls_back(self):
        _, fallback = on_screen.find("what is the", SEGMENTS, 60, 5, 1.0)
        self.assertTrue(fallback)

    def test_fallback_without_duration(self):
        self.assertEqual(on_screen.fallback_times(None, 5), [0.0])


class Merging(unittest.TestCase):
    def test_adjacent_segments_merge(self):
        segs = [seg(10.0, 12.0, "The chart here", 1), seg(14.0, 16.0, "shows the chart again", 1),
                seg(21.0, 23.0, "another chart", 1)]
        matches, _ = on_screen.find("chart", segs, 60, 5, 1.0)
        self.assertEqual(len(matches), 2)  # 14-12 = 2 s merges; 21-16 = 5 s doesn't
        self.assertEqual(matches[0]["start"], 10.0)
        self.assertEqual(matches[0]["end"], 16.0)
        self.assertEqual(matches[0]["text"], "The chart here shows the chart again")
        self.assertEqual(matches[0]["speaker"], 1)
        self.assertEqual(matches[1]["start"], 21.0)

    def test_mixed_speakers_drop_speaker(self):
        segs = [seg(10.0, 12.0, "The chart", 1), seg(13.0, 15.0, "That chart", 2)]
        matches, _ = on_screen.find("chart", segs, 60, 5, 1.0)
        self.assertEqual(len(matches), 1)
        self.assertNotIn("speaker", matches[0])


class OffsetClamping(unittest.TestCase):
    def test_clamped_to_segment_end(self):
        self.assertEqual(on_screen.frame_time({"start": 10.0, "end": 10.5}, 1.0, 60), 10.5)

    def test_clamped_to_video_end(self):
        self.assertEqual(on_screen.frame_time({"start": 59.0, "end": 61.0}, 1.0, 60), 59.5)

    def test_default_offset(self):
        self.assertEqual(on_screen.frame_time({"start": 10.0, "end": 14.0}, 1.0, 60), 11.0)

    def test_segment_starting_at_end(self):
        self.assertEqual(on_screen.frame_time({"start": 60.0, "end": 60.0}, 1.0, 60), 59.5)

    def test_find_uses_clamped_time(self):
        segs = [seg(58.8, 60.0, "the final price")]
        matches, _ = on_screen.find("price", segs, 60, 5, 2.0)
        self.assertEqual(matches[0]["frame_time"], 59.5)


class CachedTranscriptReuse(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = mock.patch.object(transcript_cache, "PATH", os.path.join(self.tmp.name, "t.sqlite"))
        self.path.start()

    def tearDown(self):
        self.path.stop()
        self.tmp.cleanup()

    def test_second_call_uses_cache(self):
        meta = {"title": "Demo", "url": "https://youtu.be/x", "platform": "youtube", "duration": 60,
                "video_key": "Youtube:x"}
        transcript = {"transcript": "Paper trading.", "segments": [seg(1.0, 3.0, "Paper trading.")],
                      "language": "en", "audio_s": 60.0}
        with mock.patch.object(pipeline.media, "probe", side_effect=lambda *a, **k: dict(meta)) as probe, \
                mock.patch.object(pipeline.media, "download_audio", return_value="/tmp/x.mp3") as dl, \
                mock.patch.object(pipeline.deepgram, "transcribe", return_value=transcript) as dg:
            first = pipeline.transcribe_cached("https://youtu.be/x", "t1")
            second = pipeline.transcribe_cached("https://www.youtube.com/watch?v=x", "t2")
        self.assertEqual(dg.call_count, 1)
        self.assertEqual(dl.call_count, 1)
        self.assertEqual(probe.call_count, 2)  # metadata is always fresh
        self.assertNotIn("cached", first)
        self.assertTrue(second["cached"])
        self.assertEqual(second["segments"], transcript["segments"])
        self.assertEqual(second["title"], "Demo")
        self.assertNotIn("video_key", second)

    def test_section_is_not_cached(self):
        meta = {"title": "Demo", "duration": 60, "video_key": "Youtube:y"}
        transcript = {"transcript": "Hi.", "segments": [seg(0.0, 1.0, "Hi.")], "language": "en", "audio_s": 5}
        with mock.patch.object(pipeline.media, "probe", side_effect=lambda *a, **k: dict(meta)), \
                mock.patch.object(pipeline.media, "download_audio", return_value="/tmp/y.mp3"), \
                mock.patch.object(pipeline.deepgram, "transcribe",
                                  side_effect=lambda *a: {**transcript, "segments": [dict(s) for s in transcript["segments"]]}) as dg:
            pipeline.transcribe_cached("u", "t1", start=10, end=20)
            again = pipeline.transcribe_cached("u", "t2")
        self.assertEqual(dg.call_count, 2)
        self.assertNotIn("cached", again)


# Fake embeddings: each text points along the "meaning" axes its words belong to; text
# with none of them gets an axis of its own, so it is unrelated to everything else.
AXES = {"elephant": 0, "trunk": 0, "pump": 0, "pric": 1, "cost": 1, "£": 1, "weather": 2}


def fake_vec(text):
    v = [0.0, 0.0, 0.0, 0.0]
    for word, axis in AXES.items():
        if word in text.lower():
            v[axis] = 1.0
    if not any(v):
        v[3] = 1.0
    n = sum(x * x for x in v) ** 0.5
    return [x / n for x in v]


ZOO = [seg(1.0, 4.0, "So here we are, one of the elephants."),
       seg(5.0, 9.0, "They have really long pumps."),
       seg(12.0, 14.0, "It costs £49 a month to visit."),
       seg(20.0, 22.0, "Nice weather today.")]


class HybridMatching(unittest.TestCase):
    def matcher(self, **kw):
        return on_screen.HybridMatcher(fake_vec, lambda texts: [fake_vec(t) for t in texts], **kw)

    def test_meaning_finds_what_keywords_miss(self):
        self.assertEqual(on_screen.KeywordMatcher().match("pricing", ZOO), [])
        matches, fallback = on_screen.find("pricing", ZOO, 30, 5, 1.0, self.matcher())
        self.assertFalse(fallback)
        self.assertEqual([m["start"] for m in matches], [12.0])

    def test_misheard_word(self):
        matches, _ = on_screen.find("trunks", ZOO, 30, 5, 1.0, self.matcher())
        # The "pumps" line is found (merged with the elephants line 1 s before it).
        self.assertTrue(any("pumps" in m["text"] for m in matches))
        self.assertNotIn(12.0, [m["start"] for m in matches])

    def test_unrelated_query_falls_back(self):
        _, fallback = on_screen.find("submarine", ZOO, 30, 5, 1.0, self.matcher())
        self.assertTrue(fallback)

    def test_keyword_hit_ranks_above_meaning_only(self):
        hits = dict(self.matcher().match("elephants", ZOO))
        self.assertGreater(hits[0], hits[1])  # says "elephants" vs only means it

    def test_embedding_failure_falls_back_to_keywords(self):
        def broken(_):
            raise embeddings.EmbeddingError("Voyage HTTP 500")
        m = on_screen.HybridMatcher(broken, broken)
        matches, fallback = on_screen.find("weather", ZOO, 30, 5, 1.0, m)
        self.assertFalse(fallback)
        self.assertEqual([x["start"] for x in matches], [20.0])
        _, fallback = on_screen.find("pricing", ZOO, 30, 5, 1.0, m)
        self.assertTrue(fallback)


class EmbeddingCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = mock.patch.object(transcript_cache, "PATH", os.path.join(self.tmp.name, "t.sqlite"))
        self.path.start()

    def tearDown(self):
        self.path.stop()
        self.tmp.cleanup()

    def test_lines_embedded_once(self):
        def fake_post(url, json, **kw):
            resp = mock.Mock(status_code=200)
            resp.json.return_value = {"data": [{"index": i, "embedding": [3.0, 4.0]} for i in range(len(json["input"]))],
                                      "usage": {"total_tokens": 7}}
            return resp
        with mock.patch.object(embeddings.requests, "post", side_effect=fake_post) as post:
            first = embeddings.embed_lines(["a", "b"])
            second = embeddings.embed_lines(["a", "b"])
            embeddings.embed_lines(["a", "c"])  # different transcript: embedded again
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0].kwargs["json"]["input_type"], "document")
        self.assertAlmostEqual(first[0][0], 0.6, places=5)  # normalised
        self.assertEqual([[round(x, 5) for x in v] for v in second], [[0.6, 0.8], [0.6, 0.8]])

    def test_http_error_raises(self):
        with mock.patch.object(embeddings.requests, "post", return_value=mock.Mock(status_code=401, text="bad key")):
            with self.assertRaises(embeddings.EmbeddingError):
                embeddings.embed_query("x")


if __name__ == "__main__":
    unittest.main()
