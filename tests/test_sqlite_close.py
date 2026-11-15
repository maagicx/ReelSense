"""The SQLite caches close every connection they open (an open handle locks the file on
Windows), and still commit their writes. Uses a temporary file; no network.

    .venv/bin/python -m unittest discover tests
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import embeddings  # noqa: E402
import transcript_cache  # noqa: E402
from music_id import cache as music_cache  # noqa: E402


class Closes(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        self.addCleanup(os.remove, self.path)  # fails on Windows if a handle stays open
        self.opened = []
        real = sqlite3.connect

        def connect(*args, **kwargs):
            con = real(*args, **kwargs)
            self.opened.append(con)
            return con
        patcher = mock.patch("sqlite3.connect", side_effect=connect)
        patcher.start()
        self.addCleanup(patcher.stop)

    def assert_all_closed(self):
        self.assertTrue(self.opened)
        for con in self.opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                con.execute("SELECT 1")

    def test_transcript_cache(self):
        with mock.patch.object(transcript_cache, "PATH", self.path):
            transcript_cache.put("k", {"transcript": "hi"})
            self.assertEqual(transcript_cache.get("k"), {"transcript": "hi"})
        self.assert_all_closed()

    def test_music_cache(self):
        with mock.patch.object(music_cache, "PATH", self.path):
            music_cache.put("k", {"tracks": []}, 60)
            self.assertEqual(music_cache.get("k"), {"tracks": []})
            music_cache.add_requests(2)
            self.assertEqual(music_cache.requests_this_month(), 2)
        self.assert_all_closed()

    def test_embeddings(self):
        with mock.patch.object(transcript_cache, "PATH", self.path):
            embeddings._store("k", [[1.0, 0.0]])
            self.assertEqual(embeddings._load("k", 1), [[1.0, 0.0]])
        self.assert_all_closed()


if __name__ == "__main__":
    unittest.main()
