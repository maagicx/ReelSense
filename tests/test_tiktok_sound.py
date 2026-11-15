"""TikTok's own-sound shortcut in media.download_audio: the post's sound file stands in
for the video's audio only when it is the creator's own and the same length. No network.

    .venv/bin/python -m unittest discover tests
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import media  # noqa: E402

AUDIO = {"format_id": "audio", "url": "https://music.example/a.mp3"}
VIDEO = {"format_id": "h264_540p", "url": "https://video.example/v.mp4"}


def info(**kw):
    return {"uploader": "dylan.page", "channel": "Dylan Page", "track": "original sound",
            "artists": ["Dylan Page"], "duration": 79, "formats": [dict(AUDIO), dict(VIDEO)], **kw}


def kept(pp, i):
    _, out = pp.run(i)
    return [f["format_id"] for f in out["formats"]]


class SoundChoice(unittest.TestCase):
    def test_own_sound_keeps_audio(self):
        pp = media._TikTokSound(allow=True)
        self.assertEqual(kept(pp, info()), ["audio", "h264_540p"])
        self.assertTrue(pp.used)

    def test_handle_match_counts(self):
        pp = media._TikTokSound(allow=True)
        kept(pp, info(channel="Someone", artists=["DYLAN.PAGE"]))
        self.assertTrue(pp.used)

    def test_someone_elses_sound_is_dropped(self):
        pp = media._TikTokSound(allow=True)
        self.assertEqual(kept(pp, info(artists=["tiff"])), ["h264_540p"])
        self.assertFalse(pp.used)

    def test_licensed_track_is_dropped(self):
        pp = media._TikTokSound(allow=True)
        self.assertEqual(kept(pp, info(track="Eine kleine Nachtmusik", artists=["Mozart"])), ["h264_540p"])

    def test_not_allowed_or_no_duration(self):
        for pp, i in ((media._TikTokSound(allow=False), info()), (media._TikTokSound(allow=True), info(duration=None))):
            self.assertEqual(kept(pp, i), ["h264_540p"])
            self.assertFalse(pp.used)


class LengthCheck(unittest.TestCase):
    """download_audio keeps the sound file only if it is the video's length."""
    def run_download(self, sound_length, own=True):
        calls = []

        def fake_download(url, job_id, section, music, sound):
            calls.append(sound.allow if sound else None)
            if sound and sound.allow:
                sound.used, sound.duration = own, 79
            return "/tmp/x.mp3"

        with mock.patch.object(media, "_download_audio", side_effect=fake_download), \
                mock.patch.object(media, "_audio_length", return_value=sound_length), \
                mock.patch.object(media, "normalise_url", side_effect=lambda u: u), \
                mock.patch.object(media, "_rm"), self.assertLogs("transcribe-mcp", "INFO") as logs:
            media.download_audio("https://www.tiktok.com/@a/video/1", "j1")
            media.log.info("end")
        return calls, "\n".join(logs.output)

    def test_matching_length_keeps_the_sound(self):
        calls, logs = self.run_download(79.6)
        self.assertEqual(calls, [True])
        self.assertIn("own sound file", logs)

    def test_wrong_length_falls_back_to_the_video(self):
        calls, logs = self.run_download(187.0)
        self.assertEqual(calls, [True, False])
        self.assertIn("downloading the video instead", logs)

    def test_unreadable_length_falls_back(self):
        self.assertEqual(self.run_download(None)[0], [True, False])

    def test_not_own_sound_downloads_once(self):
        self.assertEqual(self.run_download(187.0, own=False)[0], [True])


if __name__ == "__main__":
    unittest.main()
