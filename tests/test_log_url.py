"""Links in log lines: no tracking tags, tokens, logins or fragments. No network.

    .venv/bin/python -m unittest discover tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import media  # noqa: E402


class LogUrl(unittest.TestCase):
    def test_keeps_video_id(self):
        self.assertEqual(media.log_url("https://www.youtube.com/watch?v=abc123&si=TRACK&t=42#x"),
                         "https://www.youtube.com/watch?v=abc123")
        self.assertEqual(media.log_url("https://www.youtube.com/playlist?list=PL1&pp=zzz"),
                         "https://www.youtube.com/playlist?list=PL1")

    def test_drops_query_fragment_and_login(self):
        self.assertEqual(media.log_url("https://user:pw@cdn.example.com:8443/v.mp4?token=SECRET&exp=1#f"),
                         "https://cdn.example.com:8443/v.mp4")
        self.assertEqual(
            media.log_url("https://www.tiktok.com/@a/video/1?is_from_webapp=1&sender_device=pc"),
            "https://www.tiktok.com/@a/video/1")

    def test_clips_long_links(self):
        self.assertTrue(media.log_url("https://example.com/" + "a" * 500).endswith("…"))

    def test_links_inside_error_text(self):
        msg = "ERROR: HTTP Error 403 on https://v16.tiktokcdn.com/x.mp4?sig=SECRET&exp=9 (try again)"
        out = media.log_text(msg, 500)
        self.assertNotIn("SECRET", out)
        self.assertIn("https://v16.tiktokcdn.com/x.mp4 (try again)", out)


if __name__ == "__main__":
    unittest.main()
