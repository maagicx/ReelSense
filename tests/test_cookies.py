"""The cookie policy in media._ytdl: YouTube tries without cookies first and retries
with them; every other site, TikTok included, gets them straight away. yt-dlp is mocked: no network.

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
    """Records the cookiefile of each run; fails the runs listed in `fail` with `error`,
    and sends yt-dlp's stale-cookies warning on the runs listed in `stale`."""
    def __init__(self, fail=(), error="ERROR: unable to download video data: HTTP Error 403: Forbidden",
                 stale=()):
        self.calls, self.fail, self.error, self.stale = [], set(fail), error, set(stale)

    def __call__(self, opts):
        n = len(self.calls)
        self.calls.append(opts.get("cookiefile"))
        if opts.get("cookiefile"):
            assert os.path.exists(opts["cookiefile"]), "cookie copy missing during the run"
        if n in self.stale:
            opts["logger"].warning("[youtube] The provided YouTube account cookies are no longer valid.")
        ydl = mock.MagicMock()
        ydl.__enter__.return_value = ydl
        if n in self.fail:
            ydl.extract_info.side_effect = Exception(self.error)
        else:
            ydl.extract_info.return_value = {"id": "x", "run": n}
        return ydl


class WithCookieJar(unittest.TestCase):
    """A cookies.txt and a TMP of its own; no tests here."""
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


class CookiePolicy(WithCookieJar):

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
        for url in ("https://www.instagram.com/p/x/", "https://www.tiktok.com/@a/video/1",
                    "https://x.com/a/status/1"):
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


SIGN_IN = "ERROR: [youtube] x: Sign in to confirm your age. This video may be inappropriate for some users."


class CookieMessages(WithCookieJar):
    """What the user is told when the server's cookies are, or may be, to blame."""
    def error_for(self, target, fake):
        with self.assertRaises(UserError) as ctx, self.assertLogs("transcribe-mcp", "INFO") as logs:
            self.run_with(target, fake)
        return str(ctx.exception), "\n".join(logs.output)

    def test_stale_youtube_cookies(self):
        msg, logs = self.error_for("https://www.youtube.com/watch?v=x", FakeYDL(fail={0, 1}, stale={1}))
        self.assertEqual(msg, "The server's YouTube cookies have expired, so YouTube refused the request. "
                              "Ask the server's admin to refresh its cookies.txt.")
        self.assertIn("[cookies] YouTube", logs)

    def test_sign_in_with_cookies(self):
        msg, _ = self.error_for("https://www.youtube.com/watch?v=x", FakeYDL(fail={0, 1}, error=SIGN_IN))
        self.assertIn("cookies didn't pass it", msg)
        self.assertIn("refresh its cookies.txt", msg)

    def test_instagram_login_names_the_site(self):
        fake = FakeYDL(fail={0}, error="ERROR: [Instagram] x: Requested content is not available, "
                                       "rate-limit reached or login required")
        msg, _ = self.error_for("https://www.instagram.com/reel/x/", fake)
        self.assertTrue(msg.startswith("Instagram asked for a sign-in"), msg)

    def test_sign_in_without_cookie_file(self):
        with mock.patch.object(media, "COOKIES_FILE", None), \
                mock.patch.object(media.yt_dlp, "YoutubeDL", FakeYDL(fail={0}, error=SIGN_IN)), \
                self.assertRaises(UserError) as ctx:
            media._ytdl("https://www.youtube.com/watch?v=x", {}, "t1")
        self.assertIn("this server has no cookies.txt", str(ctx.exception))

    def test_tiktok_403_with_cookies_blames_them(self):
        msg, logs = self.error_for("https://www.tiktok.com/@a/video/1", FakeYDL(fail={0}))
        self.assertEqual(msg, "TikTok refused the download (HTTP 403) even with the server's cookies: "
                              "its TikTok cookies are probably missing or expired. "
                              "Ask the server's admin to refresh its cookies.txt.")
        self.assertIn("[cookies] TikTok", logs)
        self.assertIn("unable to download video data", logs)
        self.assertNotIn("unrecognised", logs)

    def test_403_without_cookie_file(self):
        with mock.patch.object(media, "COOKIES_FILE", None), \
                mock.patch.object(media.yt_dlp, "YoutubeDL", FakeYDL(fail={0})), \
                self.assertRaises(UserError) as ctx:
            media._ytdl("https://www.tiktok.com/@a/video/1", {}, "t1")
        self.assertTrue(str(ctx.exception).startswith("TikTok refused the download (HTTP 403). "
                                                      "This is often temporary"), str(ctx.exception))

    def test_other_failures_dont_blame_cookies(self):
        fake = FakeYDL(fail={0, 1}, error="ERROR: [youtube] x: Video unavailable")
        with mock.patch.object(media.yt_dlp, "YoutubeDL", fake), self.assertRaises(UserError) as ctx:
            media._ytdl("https://www.youtube.com/watch?v=x", {}, "t1")
        self.assertEqual(str(ctx.exception), "The video is private, removed or unavailable.")


if __name__ == "__main__":
    unittest.main()
