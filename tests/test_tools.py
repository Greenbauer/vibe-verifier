"""gates/_tools.py's download of a pinned release asset: a fetch the release host fails in passing is
tried again, a refusal and a digest mismatch are not. The fetch and the wait between attempts are
stubbed, so nothing here touches the network or sleeps."""
import hashlib
import http.client
import io
import os
import platform
import shutil
import sys
import tempfile
import unittest
import urllib.error
import zipfile
from unittest import mock

from helpers import ROOT

sys.path.insert(0, str(ROOT / "gates"))
import _tools  # noqa: E402
from _contract import CannotRun  # noqa: E402

sys.path.remove(str(ROOT / "gates"))

PAYLOAD = b"#!/bin/sh\necho stand-in\n"
URL = "https://releases.example/stand-in/1.2.3/stand-in.zip"


def archive(payload):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as bundle:
        bundle.writestr("stand-in", payload)
    return data.getvalue()


ASSET = archive(PAYLOAD)  # built once: a zip records the time it was written, so a second build has another digest
OTHER = archive(b"#!/bin/sh\necho not the pinned build\n")


def status(code, reason):
    return urllib.error.HTTPError(URL, code, reason, {}, io.BytesIO(b""))


class Download(unittest.TestCase):
    def setUp(self):
        cache = tempfile.mkdtemp(prefix="vv-tools-")
        self.addCleanup(shutil.rmtree, cache, True)
        self.target = os.path.join(cache, "stand-in-1.2.3", "stand-in")
        self.tool = {"version": "1.2.3", "url": "https://releases.example/stand-in/{version}/{asset}",
                     "assets": {(platform.system(), platform.machine()):
                                ("stand-in.zip", hashlib.sha256(ASSET).hexdigest())}}

    def download(self, *answers):
        """Run the download with the fetch answering `answers` in order (an exception is raised, bytes
        are the body). Returns (the CannotRun message or None, fetches made, seconds waited between them)."""
        replies = [answer if isinstance(answer, Exception) else io.BytesIO(answer) for answer in answers]
        with mock.patch.object(_tools.urllib.request, "urlopen", side_effect=replies) as fetch, \
                mock.patch.object(_tools.time, "sleep") as sleep:
            try:
                _tools._download("stand-in", self.tool, self.target)
                message = None
            except CannotRun as error:
                message = str(error)
        self.assertTrue(all(call.args == (URL,) for call in fetch.call_args_list), fetch.call_args_list)
        return message, fetch.call_count, [call.args[0] for call in sleep.call_args_list]

    def test_it_succeeds_after_one_500(self):
        message, fetches, waits = self.download(status(500, "Internal Server Error"), ASSET)
        self.assertIsNone(message)
        self.assertEqual((fetches, waits), (2, [_tools.FETCH_BACKOFF]))
        with open(self.target, "rb") as handle:
            self.assertEqual(handle.read(), PAYLOAD)
        self.assertTrue(os.access(self.target, os.X_OK))

    def test_a_429_and_a_body_cut_short_are_tried_again(self):
        message, fetches, waits = self.download(status(429, "Too Many Requests"),
                                                http.client.IncompleteRead(b"PK", 100), ASSET)
        self.assertIsNone(message)
        self.assertEqual((fetches, waits), (3, [_tools.FETCH_BACKOFF, _tools.FETCH_BACKOFF * 2]))
        self.assertTrue(os.path.isfile(self.target))

    def test_it_stops_at_the_bound_with_the_last_error(self):
        message, fetches, waits = self.download(urllib.error.URLError(ConnectionResetError("reset by peer")),
                                                status(502, "Bad Gateway"), status(503, "Service Unavailable"),
                                                ASSET)  # a fourth answer that must never be asked for
        self.assertEqual((fetches, waits), (3, [2, 4]))  # the numbers docs/GATE-CONTRACT.md gives
        self.assertEqual(message, "could not fetch stand-in 1.2.3 (%s) after 3 attempts: HTTP Error 503: Service "
                                  "Unavailable; install it on PATH or set VIBE_VERIFIER_TOOLS to a cache that has it" % URL)
        self.assertFalse(os.path.exists(self.target))

    def test_a_404_is_not_tried_again(self):
        message, fetches, waits = self.download(status(404, "Not Found"), ASSET)
        self.assertEqual((fetches, waits), (1, []))
        self.assertEqual(message, "could not fetch stand-in 1.2.3 (%s) after 1 attempt: HTTP Error 404: Not Found; "
                                  "install it on PATH or set VIBE_VERIFIER_TOOLS to a cache that has it" % URL)
        self.assertFalse(os.path.exists(self.target))

    def test_no_other_4xx_is_tried_again(self):
        for code in (400, 403, 408, 499):
            with self.subTest(code=code):
                message, fetches, waits = self.download(status(code, "Refused"), ASSET)
                self.assertEqual((fetches, waits), (1, []))
                self.assertIn("after 1 attempt: HTTP Error %d: Refused" % code, message)

    def test_a_url_that_cannot_be_parsed_is_not_tried_again(self):
        message, fetches, waits = self.download(ValueError("unknown url type: 'stand-in.zip'"), ASSET)
        self.assertEqual((fetches, waits), (1, []))
        self.assertIn("after 1 attempt: unknown url type: 'stand-in.zip'", message)

    def test_a_digest_mismatch_is_not_tried_again(self):
        message, fetches, waits = self.download(OTHER, ASSET)
        self.assertEqual((fetches, waits), (1, []))
        self.assertEqual(message, "stand-in.zip does not match its pinned sha256 (got %s, pinned %s); refusing to run it"
                         % (hashlib.sha256(OTHER).hexdigest(), hashlib.sha256(ASSET).hexdigest()))
        self.assertFalse(os.path.exists(self.target))

    def test_the_digest_is_checked_on_the_attempt_that_succeeded(self):
        message, fetches, waits = self.download(status(500, "Internal Server Error"), OTHER, ASSET)
        self.assertEqual((fetches, waits), (2, [_tools.FETCH_BACKOFF]))
        self.assertIn("does not match its pinned sha256", message)
        self.assertFalse(os.path.exists(self.target))


if __name__ == "__main__":
    unittest.main()
