"""A job's steps (media.Progress): what the user is shown of their own job. Network-free.

    .venv/bin/python -m unittest discover tests
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import media  # noqa: E402
import pipeline  # noqa: E402
import transcript_cache  # noqa: E402
from test_cookies import FakeYDL, WithCookieJar  # noqa: E402

META = {"title": "Demo", "platform": "tiktok", "duration": 82, "video_key": "TikTok:1"}
TRANSCRIPT = {"transcript": "Hello there.", "language": "en", "audio_s": 82.0,
              "segments": [{"start": 0.0, "end": 1.0, "text": "Hello there."}]}


class Tracked(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(transcript_cache, "PATH", os.path.join(self.tmp.name, "t.sqlite"))
        patch.start()
        self.addCleanup(patch.stop)
        self.progress = media.Progress()
        token = media.track(self.progress)
        self.addCleanup(media.untrack, token)

    def run_job(self, fail_download=False):
        dl = mock.patch.object(pipeline.media, "download_audio",
                               side_effect=media.UserError("refused") if fail_download else None,
                               return_value="/nonexistent.mp3")
        with mock.patch.object(pipeline.media, "probe", side_effect=lambda *a, **k: dict(META)), dl, \
                mock.patch.object(pipeline.deepgram, "transcribe", return_value=dict(TRANSCRIPT)):
            return pipeline.transcribe_cached("https://www.tiktok.com/@a/video/1", "j1")

    def test_steps_of_a_full_job(self):
        with self.assertLogs("transcribe-mcp", "INFO") as logs:
            self.run_job()
        steps = self.progress.steps
        self.assertEqual(len(steps), 3, steps)
        self.assertTrue(steps[0].startswith("read details: tiktok, 82s"), steps)
        self.assertTrue(steps[1].startswith("downloaded audio:"), steps)
        self.assertTrue(steps[2].startswith("transcribed (") and "1 segments" in steps[2], steps)
        self.assertIn("'Demo'", "\n".join(logs.output))  # the title goes to the log only
        self.assertNotIn("Demo", " ".join(steps))
        self.assertIsNone(self.progress.now)

    def test_cache_hit(self):
        self.run_job()
        self.progress.steps.clear()
        self.run_job()
        self.assertEqual(self.progress.steps[1], "transcript from cache")

    def test_failure_keeps_the_steps_so_far_and_what_it_was_doing(self):
        with self.assertRaises(media.UserError):
            self.run_job(fail_download=True)
        self.assertEqual(len(self.progress.steps), 1)
        self.assertEqual(self.progress.status(), "downloading the audio")



class Untracked(unittest.TestCase):
    def test_steps_outside_a_job_only_log(self):
        done = media.Progress()
        media.untrack(media.track(done))
        with self.assertLogs("transcribe-mcp", "INFO"):
            media.step("x", "a step")
            media.doing("something")
        self.assertEqual((done.steps, done.now), ([], None))


class CookieRetryStep(WithCookieJar):
    def test_retry_is_a_step_without_the_raw_error(self):
        progress = media.Progress()
        token = media.track(progress)
        self.addCleanup(media.untrack, token)
        with self.assertLogs("transcribe-mcp", "INFO") as logs:
            self.run_with("https://www.youtube.com/watch?v=x", FakeYDL(fail={0}))
        self.assertEqual(progress.steps, ["YouTube without cookies failed, retrying with cookies"])
        self.assertIn("HTTP Error 403", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
