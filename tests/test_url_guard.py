"""The public-address check on links and card images. DNS is mocked: no network.

    .venv/bin/python -m unittest discover tests
"""
import os
import socket
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import media  # noqa: E402
from media import UserError  # noqa: E402


def resolves_to(*addrs):
    def fake(host, *args, **kwargs):
        if host == "nowhere.invalid":
            raise socket.gaierror("no such host")
        return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0))
                for a in addrs]
    return mock.patch("media.socket.getaddrinfo", side_effect=fake)


class PublicHost(unittest.TestCase):
    def test_public(self):
        with resolves_to("142.250.180.14", "2a00:1450:4009:81f::200e"):
            self.assertTrue(media.is_public_host("youtube.com"))

    def test_private_and_local(self):
        for addr in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "172.17.0.1", "169.254.169.254",
                     "100.101.102.103", "0.0.0.0", "::1", "fe80::1%eth0", "fd00::1",
                     "::ffff:127.0.0.1", "224.0.0.1"):
            with self.subTest(addr=addr), resolves_to(addr):
                self.assertFalse(media.is_public_host("example.com"))

    def test_any_private_answer_fails(self):
        with resolves_to("93.184.216.34", "10.0.0.1"):
            self.assertFalse(media.is_public_host("example.com"))

    def test_unresolvable_or_empty(self):
        with resolves_to():
            self.assertFalse(media.is_public_host("nowhere.invalid"))
            self.assertFalse(media.is_public_host(""))
            self.assertFalse(media.is_public_host(None))


class NormaliseUrl(unittest.TestCase):
    def test_public_link_passes(self):
        with resolves_to("142.250.180.14"):
            self.assertEqual(media.normalise_url(" https://youtu.be/abc "), "https://youtu.be/abc")

    def test_private_link_refused(self):
        for url in ("http://localhost:8010/healthz", "http://192.168.1.1/", "http://host.docker.internal/"):
            with self.subTest(url=url), resolves_to("192.168.65.254"), self.assertRaises(UserError):
                media.normalise_url(url)


class ImageDataUri(unittest.TestCase):
    def response(self, status=200, headers=None, body=b"img"):
        r = mock.MagicMock()
        r.__enter__.return_value = r
        r.is_redirect = status in (301, 302, 303, 307, 308)
        r.headers = headers or {"content-type": "image/png"}
        r.raw.read.return_value = body
        r.raise_for_status.return_value = None
        return r

    def test_redirect_to_private_refused(self):
        redirect = self.response(302, {"location": "https://internal.example/x.png"})
        def fake_dns(host, *a, **k):
            addr = "10.0.0.1" if host == "internal.example" else "93.184.216.34"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 0))]
        with mock.patch("media.socket.getaddrinfo", side_effect=fake_dns), \
                mock.patch("media.requests.get", return_value=redirect) as get:
            self.assertIsNone(media.image_data_uri("https://cdn.example/a.png"))
        self.assertEqual(get.call_count, 1)

    def test_public_image_fetched(self):
        with resolves_to("93.184.216.34"), mock.patch("media.requests.get", return_value=self.response()):
            self.assertEqual(media.image_data_uri("https://cdn.example/a.png"), "data:image/png;base64,aW1n")

    def test_private_image_not_requested(self):
        with resolves_to("127.0.0.1"), mock.patch("media.requests.get") as get:
            self.assertIsNone(media.image_data_uri("https://localhost/a.png"))
        get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
