"""TikTok slideshows (photo posts) and short links: short links are resolved, slideshows
are refused for speech and frames, and identify_music reads them as video links and uses
their sound file. Handles and IDs are made up. No network.

    .venv/bin/python -m unittest discover tests
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import media  # noqa: E402
from media import UserError  # noqa: E402

PHOTO = "https://www.tiktok.com/@example.user/photo/7000000000000000001"
VIDEO = "https://www.tiktok.com/@example.user/video/7000000000000000001"


def redirect(location):
    return mock.Mock(is_redirect=True, headers={"Location": location})


def final():
    return mock.Mock(is_redirect=False, headers={})


def public_hosts():
    return mock.patch("media.is_public_host", return_value=True)


class ShortLinks(unittest.TestCase):
    def setUp(self):
        media._resolve_tiktok_short.cache_clear()
        self.addCleanup(media._resolve_tiktok_short.cache_clear)

    def resolve(self, *responses):
        with mock.patch("media.requests.get", side_effect=list(responses)) as get:
            return media._resolve_tiktok_short("https://vm.tiktok.com/ZZabc123/"), get

    def test_resolves_to_the_post_without_tracking(self):
        url, _ = self.resolve(redirect(PHOTO + "?_r=1&_t=XYZ"))
        self.assertEqual(url, PHOTO)

    def test_follows_relative_and_t_links(self):
        url, get = self.resolve(redirect("https://www.tiktok.com/t/ZZabc123/"), redirect(VIDEO + "?_t=1"))
        self.assertEqual(url, VIDEO)
        self.assertEqual(get.call_count, 2)

    def test_refuses_a_hop_off_tiktok(self):
        with self.assertRaises(UserError):
            self.resolve(redirect("https://evil.example/x"))
        media._resolve_tiktok_short.cache_clear()
        with self.assertRaises(UserError):
            self.resolve(redirect("http://www.tiktok.com/@a/video/1"))  # not https

    def test_refuses_too_many_hops(self):
        loop = [redirect("https://vm.tiktok.com/ZZloop/")] * media.TIKTOK_SHORT_MAX_HOPS
        with self.assertRaises(UserError):
            self.resolve(*loop)

    def test_no_redirect_is_an_error(self):
        with self.assertRaises(UserError):
            self.resolve(final())

    def test_normalise_url_resolves_short_links(self):
        with public_hosts(), mock.patch("media._resolve_tiktok_short", return_value=PHOTO) as res:
            url = media.normalise_url("https://vm.tiktok.com/ZZabc123/")
        res.assert_called_once()
        self.assertTrue(url.startswith(PHOTO + "?is_from_webapp=1"))

    def test_only_short_links_are_resolved(self):
        self.assertTrue(media._is_tiktok_short("https://vt.tiktok.com/ZZ1/"))
        self.assertTrue(media._is_tiktok_short("https://www.tiktok.com/t/ZZ1/"))
        self.assertFalse(media._is_tiktok_short(VIDEO))
        self.assertFalse(media._is_tiktok_short("https://vm.tiktok.com.evil.example/ZZ1/"))


class Probe(unittest.TestCase):
    INFO = {"id": "7000000000000000001", "extractor_key": "TikTok", "duration": 60,
            "webpage_url": VIDEO, "uploader": "example.user", "track": "Some Song - Remix",
            "artist": "Some Artist", "title": "slides"}

    def probe(self, **kw):
        with public_hosts(), mock.patch("media._ytdl", return_value=dict(self.INFO)) as ytdl:
            return media.probe(PHOTO, "t", with_key=True, **kw), ytdl

    def test_refused_by_default(self):
        with public_hosts(), mock.patch("media._ytdl") as ytdl:
            with self.assertRaises(UserError) as ctx:
                media.probe(PHOTO, "t")
        self.assertIn("identify_music", str(ctx.exception))
        ytdl.assert_not_called()

    def test_allowed_reads_it_as_a_video_link(self):
        meta, ytdl = self.probe(allow_photo=True)
        self.assertIn("/video/7000000000000000001", ytdl.call_args.args[0])
        self.assertTrue(meta["is_photo_post"])
        self.assertEqual(meta["url"], PHOTO)
        self.assertEqual(meta["video_key"], "TikTok:7000000000000000001")
        self.assertEqual(meta["duration"], 60)

    def test_frames_refused(self):
        with self.assertRaises(UserError) as ctx:
            media._video_stream(PHOTO + "?is_from_webapp=1", "t")
        self.assertIn("no video", str(ctx.exception))


class Sound(unittest.TestCase):
    AUDIO = {"format_id": "audio", "url": "https://music.example/a.mp3"}

    def test_slideshow_keeps_a_licensed_sound(self):
        pp = media._TikTokSound(allow=True, photo=True)
        info = {"uploader": "example.user", "artists": ["Some Artist"], "duration": 60,
                "formats": [dict(self.AUDIO)]}
        _, out = pp.run(info)
        self.assertTrue(pp.used)
        self.assertEqual([f["format_id"] for f in out["formats"]], ["audio"])

    def test_download_uses_the_video_link_and_skips_the_length_check(self):
        with public_hosts(), mock.patch("media._download_audio", return_value="/tmp/x.mp3") as dl, \
                mock.patch("media._audio_length") as length:
            path = media.download_audio(PHOTO, "job", music=True)
        self.assertEqual(path, "/tmp/x.mp3")
        self.assertIn("/video/7000000000000000001", dl.call_args.args[0])
        self.assertTrue(dl.call_args.args[4].photo)
        length.assert_not_called()


if __name__ == "__main__":
    unittest.main()
