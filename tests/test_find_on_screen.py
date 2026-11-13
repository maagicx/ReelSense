"""find_on_screen's matching and the transcript cache. No network, no server:

    .venv/bin/python -m unittest discover tests
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

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


if __name__ == "__main__":
    unittest.main()
