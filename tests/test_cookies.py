"""The cookie policy in media._ytdl: YouTube tries without cookies first and retries
with them; every other site gets them straight away. yt-dlp is mocked: no network.

    .venv/bin/python -m unittest discover tests
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import media  # noqa: E402
from media import UserError  # noqa: E402


class FakeYDL:
    """Records the cookiefile of each run; fails the runs listed in `fail`."""
    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)

    def __call__(self, opts):
        n = len(self.calls)
        self.calls.append(opts.get("cookiefile"))
        if opts.get("cookiefile"):
            assert os.path.exists(opts["cookiefile"]), "cookie copy missing during the run"
        ydl = mock.MagicMock()
        ydl.__enter__.return_value = ydl
        if n in self.fail:
            ydl.extract_info.side_effect = Exception("ERROR: unable to download video data: HTTP Error 403: Forbidden")
        else:
            ydl.extract_info.return_value = {"id": "x", "run": n}
        return ydl


class CookiePolicy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        jar = os.path.join(self.tmp.name, "cookies.txt")
        with open(jar, "w") as f:
            f.write("# Netscape HTTP Cookie File\n")
        for patch in (mock.patch.object(media, "COOKIES_FILE", jar),
                      mock.patch.object(media, "TMP", self.tmp.name)):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def run_with(self, target, fake):
        with mock.patch.object(media.yt_dlp, "YoutubeDL", fake):
            return media._ytdl(target, {"quiet": True}, "t1")

    def leftover_copies(self):
        return [f for f in os.listdir(self.tmp.name) if f != "cookies.txt"]

    def test_youtube_public_needs_no_cookies(self):
        fake = FakeYDL()
        self.assertEqual(self.run_with("https://www.youtube.com/watch?v=x", fake)["run"], 0)
        self.assertEqual(fake.calls, [None])

    def test_youtube_retries_with_cookies(self):
        fake = FakeYDL(fail={0})
        self.assertEqual(self.run_with("https://youtu.be/x", fake)["run"], 1)
        self.assertIsNone(fake.calls[0])
        self.assertTrue(fake.calls[1].endswith("t1.cookies.txt"))

    def test_youtube_search_counts_as_youtube(self):
        fake = FakeYDL(fail={0})
        self.run_with("ytsearch5:cats", fake)
        self.assertEqual(len(fake.calls), 2)

    def test_youtube_both_fail_gives_user_error(self):
        fake = FakeYDL(fail={0, 1})
        with self.assertRaises(UserError):
            self.run_with("https://www.youtube.com/watch?v=x", fake)
        self.assertEqual(len(fake.calls), 2)

    def test_other_sites_get_cookies_first_and_once(self):
        for url in ("https://www.instagram.com/p/x/", "https://www.tiktok.com/@a/video/1"):
            with self.subTest(url=url):
                fake = FakeYDL(fail={0})
                with self.assertRaises(UserError):
                    self.run_with(url, fake)
                self.assertEqual(len(fake.calls), 1)
                self.assertIsNotNone(fake.calls[0])

    def test_no_cookie_file_means_one_plain_run(self):
        with mock.patch.object(media, "COOKIES_FILE", None):
            fake = FakeYDL(fail={0})
            with self.assertRaises(UserError):
                self.run_with("https://www.youtube.com/watch?v=x", fake)
        self.assertEqual(fake.calls, [None])

    def test_cookie_copy_removed_afterwards(self):
        for fail in (set(), {0}, {0, 1}):
            with self.subTest(fail=fail):
                try:
                    self.run_with("https://www.youtube.com/watch?v=x", FakeYDL(fail=fail))
                except UserError:
                    pass
                self.assertEqual(self.leftover_copies(), [])


if __name__ == "__main__":
    unittest.main()
