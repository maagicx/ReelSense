"""What ReelSense sends to Deepgram and how its errors read. requests.post is mocked:
no network, no key needed.

    .venv/bin/python -m unittest discover tests
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import deepgram  # noqa: E402
from media import UserError  # noqa: E402

OK_BODY = {"metadata": {"duration": 1.0},
           "results": {"channels": [{"alternatives": [{"transcript": "", "words": []}]}], "utterances": []}}


def response(status=200, body=None):
    resp = mock.Mock(status_code=status, ok=status < 400)
    if isinstance(body, Exception):
        resp.json.side_effect = body
    else:
        resp.json.return_value = OK_BODY if body is None else body
    return resp


class DeepgramTest(unittest.TestCase):
    def setUp(self):
        fd, self.audio = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        self.addCleanup(os.remove, self.audio)

    def call(self, resp, language=None, **flags):
        flags = {"MIP_OPT_OUT": True, "DIARIZE": True, **flags}
        with mock.patch.multiple(deepgram, **flags), \
                mock.patch("deepgram.requests.post", return_value=resp) as post, \
                mock.patch("deepgram.time.sleep"):
            deepgram.transcribe(self.audio, language)
        return post.call_args.kwargs["params"]

    def error(self, status, body, language=None) -> str:
        with self.assertRaises(UserError) as ctx:
            self.call(response(status, body), language)
        return str(ctx.exception)


class Params(DeepgramTest):
    def test_opt_out_by_default_with_diarize_and_detection(self):
        params = self.call(response(), MIP_OPT_OUT=True, DIARIZE=True)
        self.assertEqual(params["mip_opt_out"], "true")
        self.assertEqual(params["diarize"], "true")
        self.assertEqual(params["detect_language"], "true")

    def test_opt_out_with_a_language(self):
        params = self.call(response(), "es", MIP_OPT_OUT=True)
        self.assertEqual(params["mip_opt_out"], "true")
        self.assertEqual(params["language"], "es")
        self.assertNotIn("detect_language", params)

    def test_opt_out_off(self):
        self.assertNotIn("mip_opt_out", self.call(response(), MIP_OPT_OUT=False))


class Flag(unittest.TestCase):
    def test_default_is_on(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(deepgram._flag("DEEPGRAM_MIP_OPT_OUT"))

    def test_off_values(self):
        for value in ("0", "false", "No", " OFF "):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"DEEPGRAM_MIP_OPT_OUT": value}):
                self.assertFalse(deepgram._flag("DEEPGRAM_MIP_OPT_OUT"))

    def test_on_values(self):
        for value in ("1", "true", "yes", "on"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"DEEPGRAM_MIP_OPT_OUT": value}):
                self.assertTrue(deepgram._flag("DEEPGRAM_MIP_OPT_OUT"))


class Errors(DeepgramTest):
    def test_language_error_only_when_deepgram_says_so(self):
        msg = self.error(400, {"err_msg": "No such language 'xx' for model nova-3"}, "xx")
        self.assertIn("rejected language 'xx'", msg)
        msg = self.error(400, {"err_msg": "Bad Request: failed to process audio: corrupt or unsupported data"}, "en")
        self.assertNotIn("language", msg)
        self.assertIn("corrupt or unsupported data", msg)

    def test_400_without_a_body(self):
        self.assertEqual(self.error(400, ValueError("not json")), "Deepgram rejected the request (HTTP 400).")

    def test_key_credit_and_permission(self):
        self.assertIn("API key", self.error(401, {"err_msg": "Invalid credentials."}))
        self.assertIn("out of credit", self.error(402, {}))
        msg = self.error(403, {"err_msg": "Insufficient permissions."})
        self.assertIn("lack permission", msg)
        self.assertIn("Insufficient permissions.", msg)

    def test_other_status_keeps_deepgram_reason(self):
        self.assertEqual(self.error(404, {"err_msg": "Model not found"}),
                         "Transcription failed (Deepgram HTTP 404): Model not found")


if __name__ == "__main__":
    unittest.main()
