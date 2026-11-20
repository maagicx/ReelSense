"""list_videos' rows and ranking, and channel_info's counts: likes, keywords and
thumbnails from a flat listing, ranking by views or likes (with per-video lookups where
the listing has no likes, as on YouTube), TikTok's profile stats and YouTube's upload
count. yt-dlp and the page fetch are mocked. No network.

    .venv/bin/python -m unittest discover tests
"""
import base64
import os
import shutil
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import media  # noqa: E402

YT = "https://www.youtube.com/@example"
TT = "https://www.tiktok.com/@example.user"


def yt_entry(n, views, **kw):
    return {"_type": "url", "id": f"v{n}", "url": f"https://www.youtube.com/watch?v=v{n}",
            "title": f"Video {n}", "view_count": views, "duration": 60,
            "thumbnails": [{"url": f"https://i.ytimg.com/vi/v{n}/hq720.jpg?a", "width": 360},
                           {"url": f"https://i.ytimg.com/vi/v{n}/hq720.jpg?b", "width": 720}], **kw}


def tt_entry(n, views, likes, desc=""):
    return {"_type": "url", "id": str(n), "url": f"{TT}/video/{n}", "title": desc[:20] or f"Post {n}",
            "description": desc, "view_count": views, "like_count": likes, "comment_count": 5,
            "repost_count": 2, "channel": "Example", "timestamp": 1795114496,
            "thumbnails": [{"id": "dynamicCover", "url": "https://p16.tiktokcdn.com/dyn"},
                           {"id": "cover", "url": "https://p16.tiktokcdn.com/cover"}]}


def listing(entries, **kw):
    return {"_type": "playlist", "entries": entries, **kw}


class Rows(unittest.TestCase):
    def test_tiktok_row_has_engagement_keywords_and_still_cover(self):
        row = media._flat_entry(tt_entry(1, 100, 40, "Moon day #Space #moon #space #NASA."))
        self.assertEqual((row["likes"], row["comments"], row["shares"]), (40, 5, 2))
        self.assertEqual(row["keywords"], ["Space", "moon", "NASA"])  # deduped, case-blind
        self.assertEqual(row["thumbnail"], "https://p16.tiktokcdn.com/cover")
        self.assertEqual(row["published"], "2026-11-19")

    def test_tags_win_over_hashtags_and_are_capped(self):
        tags = [f"t{i}" for i in range(30)]
        self.assertEqual(media._keywords({"tags": tags, "description": "#other"}), tags[:media.MAX_KEYWORDS])

    def test_youtube_thumbnail_is_smallest_wide_enough(self):
        self.assertEqual(media._flat_entry(yt_entry(1, 5))["thumbnail"], "https://i.ytimg.com/vi/v1/hq720.jpg?a")

    def test_missing_fields_are_left_out(self):
        row = media._flat_entry({"url": "https://www.youtube.com/watch?v=x", "title": "x"})
        self.assertEqual(row, {"url": "https://www.youtube.com/watch?v=x", "title": "x"})


class Ranking(unittest.TestCase):
    def test_tiktok_likes_ranked_from_listing_without_lookups(self):
        entries = [tt_entry(1, 900, 10), tt_entry(2, 100, 90), tt_entry(3, 500, 50)]
        with mock.patch.object(media, "_flat_extract", return_value=listing(entries)), \
                mock.patch.object(media, "_ytdl") as ytdl:
            videos, _, meta = media.list_ranked(TT, 2, "likes", "t")
        ytdl.assert_not_called()
        self.assertEqual([v["likes"] for v in videos], [90, 50])
        self.assertEqual((meta["scanned"], meta["ranked_by"], meta["channel"]), (3, "likes", "Example"))
        self.assertNotIn("likes_note", meta)

    def test_youtube_likes_looked_up_for_most_viewed_only(self):
        videos_tab = listing([yt_entry(i, views=i * 100) for i in range(1, 11)], channel="Example")
        shorts_tab = listing([yt_entry(99, views=50, url="https://www.youtube.com/shorts/s99")])
        likes = {f"https://www.youtube.com/watch?v=v{i}": 1000 - i * 10 for i in range(1, 11)}
        likes["https://www.youtube.com/watch?v=v7"] = 5000  # liked far more than its views suggest

        def ytdl(url, opts, tag):
            return {"like_count": likes.get(url), "tags": ["kw"], "upload_date": "20240102"}
        with mock.patch.object(media, "_flat_extract", side_effect=[videos_tab, shorts_tab]), \
                mock.patch.object(media, "_ytdl", side_effect=ytdl) as called, \
                mock.patch.object(media, "LIKES_LOOKUP_TOP", 4):
            videos, _, meta = media.list_ranked(YT, 2, "likes", "t")
        looked = sorted(c.args[0] for c in called.call_args_list)
        self.assertEqual(looked, sorted(f"https://www.youtube.com/watch?v=v{i}" for i in (7, 8, 9, 10)))
        self.assertEqual([v["url"][-2:] for v in videos], ["v7", "v8"])
        self.assertEqual(videos[0]["keywords"], ["kw"])
        self.assertEqual(videos[0]["published"], "2024-01-02")
        self.assertEqual(meta["scanned"], 11)
        self.assertIn("4 most viewed of 11 scanned (4 had them)", meta["likes_note"])

    def test_youtube_views_ranking_adds_likes_to_rows_shown(self):
        tab = listing([yt_entry(i, views=i) for i in range(1, 6)])
        with mock.patch.object(media, "_flat_extract", return_value=tab), \
                mock.patch.object(media, "_ytdl", return_value={"like_count": 7}) as called:
            videos, _, meta = media.list_ranked("https://www.youtube.com/playlist?list=PL1", 3, "views", "t")
        self.assertEqual(called.call_count, 3)
        self.assertEqual([(v["views"], v["likes"]) for v in videos], [(5, 7), (4, 7), (3, 7)])
        self.assertNotIn("likes_note", meta)

    def test_failed_lookup_keeps_the_row(self):
        tab = listing([yt_entry(1, views=10), yt_entry(2, views=20)])

        def ytdl(url, opts, tag):
            if url.endswith("v2"):
                raise media.UserError("gone")
            return {"like_count": 3}
        with mock.patch.object(media, "_flat_extract", return_value=tab), \
                mock.patch.object(media, "_ytdl", side_effect=ytdl):
            videos, _, meta = media.list_ranked("https://www.youtube.com/playlist?list=PL1", 5, "likes", "t")
        self.assertEqual([(v["url"][-2:], v.get("likes")) for v in videos], [("v1", 3), ("v2", None)])
        self.assertIn("(1 had them)", meta["likes_note"])


PROFILE = ('<script>{"userInfo":{"user":{"avatarThumb":"https:\\u002F\\u002Fp16.tiktokcdn.com\\u002Fa.jpeg"},'
           '"stats":{"followerCount":2200000,"followingCount":23,"heartCount":11800000,"videoCount":68},'
           '"statsV2":{"followerCount":"2206279","followingCount":"23","heart":"11826894",'
           '"heartCount":"11826894","videoCount":"68"}},'
           '"itemList":[{"stats":{"diggCount":5,"playCount":9}}]}</script>')


class Counts(unittest.TestCase):
    def test_tiktok_profile_stats_prefer_exact(self):
        page = mock.Mock(text=PROFILE, raise_for_status=lambda: None)
        with mock.patch.object(media.requests, "get", return_value=page):
            got = media.profile_page(f"{TT}/video/1")
        self.assertEqual(got["avatar"], "https://p16.tiktokcdn.com/a.jpeg")
        self.assertEqual(got["stats"], {"followers": 2206279, "following": 23, "likes": 11826894, "videos": 68})

    def test_tiktok_stats_absent(self):
        self.assertEqual(media._tiktok_stats('{"itemList":[{"stats":{"diggCount":5}}]}'), {})

    def test_youtube_channel_gets_upload_count(self):
        tab = listing([yt_entry(1, 5)], channel="Example", channel_id="UCabc", uploader_id="@example",
                      channel_follower_count=10, channel_url=f"{YT}")
        uploads = listing([], playlist_count=6207)
        with mock.patch.object(media, "_flat_extract", side_effect=[tab, uploads]) as called:
            out = media.channel(YT, "t")
        self.assertEqual(called.call_args_list[1].args[0], "https://www.youtube.com/playlist?list=UUabc")
        self.assertEqual((out["subscribers"], out["videos"]), (10, 6207))
        self.assertNotIn("note", out)

    def test_upload_count_needs_a_channel_id(self):
        self.assertIsNone(media._youtube_upload_count("not-a-UC-id", "t"))


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
class SmallThumbnail(unittest.TestCase):
    def test_big_image_is_shrunk(self):
        big = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=720x1280",
                              "-frames:v", "1", "-q:v", "1", "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1"],
                             capture_output=True, check=True).stdout
        self.assertGreater(len(big), media.LIST_THUMB_KEEP_BYTES)
        with mock.patch.object(media, "_fetch_image", return_value=("image/jpeg", big)):
            uri = media.small_image_data_uri("https://p16.tiktokcdn.com/cover")
        self.assertTrue(uri.startswith("data:image/jpeg;base64,"))
        self.assertLess(len(base64.b64decode(uri.split(",", 1)[1])), len(big) // 4)

    def test_small_image_passes_through(self):
        with mock.patch.object(media, "_fetch_image", return_value=("image/webp", b"tiny")):
            self.assertEqual(media.small_image_data_uri("https://x.example/a"),
                             f"data:image/webp;base64,{base64.b64encode(b'tiny').decode()}")

    def test_fetch_failure(self):
        with mock.patch.object(media, "_fetch_image", return_value=None):
            self.assertIsNone(media.small_image_data_uri("https://x.example/a"))


if __name__ == "__main__":
    unittest.main()
