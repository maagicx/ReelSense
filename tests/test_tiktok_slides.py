"""TikTok slideshow images for get_frames: slide links from the post page's data, mirror
fallback, slide selection and the image-host check. A made-up page; requests, probe and
ffmpeg are mocked. No network.

    .venv/bin/python -m unittest discover tests
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import media  # noqa: E402
from media import UserError  # noqa: E402

PHOTO = "https://www.tiktok.com/@example.user/photo/7000000000000000001"
CDN = "https://p16-common-sign.tiktokcdn-eu.com/obj/slide{n}-{m}.jpeg?x-sig=abc"


def page(slides=3, mirrors=2):
    item = {"id": "7000000000000000001", "imagePost": {"images": [
        {"imageURL": {"urlList": [CDN.format(n=n, m=m) for m in range(mirrors)]}} for n in range(1, slides + 1)]}}
    blob = {"__DEFAULT_SCOPE__": {"webapp.video-detail": {"itemInfo": {"itemStruct": item}}}}
    return (f'<html><script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'
            f'{json.dumps(blob)}</script></html>')


class Resp:
    def __init__(self, status=200, body=b"img", text=""):
        self.status_code, self.body, self.text = status, body, text

    def iter_content(self, size):
        yield self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class SlideUrls(unittest.TestCase):
    def test_reads_every_slide_and_its_mirrors(self):
        urls = media._slide_urls(page(3, 2))
        self.assertEqual(len(urls), 3)
        self.assertEqual(urls[1], [CDN.format(n=2, m=0), CDN.format(n=2, m=1)])

    def test_login_wall_or_missing_blob(self):
        for html in ("<html>Log in to TikTok</html>", page(0),
                     '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">{not json</script>'):
            with self.subTest(html=html[:30]), self.assertRaises(UserError):
                media._slide_urls(html)


class Pick(unittest.TestCase):
    def test_all_by_default_up_to_max(self):
        self.assertEqual(media._pick_slides(None, 3), [1, 2, 3])
        self.assertEqual(len(media._pick_slides([], media.MAX_FRAMES + 5)), media.MAX_FRAMES)

    def test_chosen_sorted_and_deduplicated(self):
        self.assertEqual(media._pick_slides([3, 1, 3], 4), [1, 3])

    def test_out_of_range_names_the_count(self):
        with self.assertRaises(UserError) as ctx:
            media._pick_slides([2], 1)
        self.assertIn("1 slide", str(ctx.exception))
        with self.assertRaises(UserError):
            media._pick_slides([0], 3)

    def test_too_many(self):
        with self.assertRaises(UserError):
            media._pick_slides(list(range(1, media.MAX_FRAMES + 2)), 50)


class Fetch(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp()
        os.close(fd)
        self.addCleanup(os.remove, self.path)
        patcher = mock.patch("media.is_public_host", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_second_mirror_when_the_first_refuses(self):
        urls = [CDN.format(n=1, m=0), CDN.format(n=1, m=1)]
        with mock.patch("media.requests.get", side_effect=[Resp(403), Resp(200, b"JPEG")]) as get:
            self.assertEqual(media._fetch_slide(urls, self.path), urls[1])
        self.assertEqual(get.call_count, 2)
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), b"JPEG")

    def test_only_tiktok_image_servers_over_https(self):
        bad = ["https://evil.example/a.jpg", "http://p16.tiktokcdn.com/a.jpg",
               "https://tiktokcdn.com.evil.example/a.jpg"]
        with mock.patch("media.requests.get") as get:
            self.assertIsNone(media._fetch_slide(bad, self.path))
        get.assert_not_called()

    def test_private_address_refused(self):
        with mock.patch("media.is_public_host", return_value=False), mock.patch("media.requests.get") as get:
            self.assertIsNone(media._fetch_slide([CDN.format(n=1, m=0)], self.path))
        get.assert_not_called()

    def test_size_cap(self):
        with mock.patch.object(media, "SLIDE_MAX_MB", 0.000001), \
                mock.patch("media.requests.get", return_value=Resp(200, b"x" * 100)):
            with self.assertRaises(UserError):
                media._fetch_slide([CDN.format(n=1, m=0)], self.path)


class Slides(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch("media.is_public_host", return_value=True),
                  mock.patch("media.probe", return_value={"title": "slides", "url": PHOTO, "is_photo_post": True}),
                  mock.patch("media._grab", side_effect=lambda path, t: b"JPG:" + os.path.basename(path).encode())):
            p.start()
            self.addCleanup(p.stop)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        tmp = mock.patch.object(media, "TMP", self.tmp)
        tmp.start()
        self.addCleanup(tmp.stop)
        self.addCleanup(lambda: media.cleanup("t"))

    def test_chosen_slides_from_the_video_page(self):
        def get(url, **kw):
            return Resp(text=page(3)) if "tiktok.com/@" in url else Resp(200, b"img")
        with mock.patch("media.requests.get", side_effect=get) as req:
            meta, got = media.slides(PHOTO, [1, 3], "t")
        self.assertIn("/video/7000000000000000001", req.call_args_list[0].args[0])
        self.assertEqual(meta["slide_count"], 3)
        self.assertEqual([n for n, _, _ in got], [1, 3])
        self.assertEqual(got[1][1], b"JPG:t-s3")
        self.assertEqual(got[1][2], CDN.format(n=3, m=0))

    def test_not_a_slideshow(self):
        with self.assertRaises(UserError) as ctx:
            media.slides("https://www.tiktok.com/@example.user/video/7000000000000000002", None, "t")
        self.assertIn("timestamps", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
