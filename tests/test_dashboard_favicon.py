"""Owner avatar tab icon with the dashboard badge, and its letter fallback."""

import base64
import io
import unittest

from dashboard.favicon import BADGE, MAX_AVATAR_BYTES, RETRY_SECONDS, Favicon, fetch_avatar, owner_color

PNG = b"\x89PNG\r\n\x1a\n" + b"pixels"


def opener_returning(body):
    calls = []

    def opener(url, timeout):
        calls.append(url)
        return io.BytesIO(body)
    opener.calls = calls
    return opener


class FetchAvatar(unittest.TestCase):
    def test_reads_the_public_owner_avatar(self):
        opener = opener_returning(PNG)
        self.assertEqual(fetch_avatar("octocat", opener), ("image/png", PNG))
        self.assertEqual(opener.calls, ["https://github.com/octocat.png?size=64"])

    def test_rejects_non_images_oversized_bodies_and_network_errors(self):
        self.assertIsNone(fetch_avatar("o", opener_returning(b"<html>")))
        self.assertIsNone(fetch_avatar("o", opener_returning(PNG + b"x" * MAX_AVATAR_BYTES)))

        def failing(url, timeout):
            raise OSError("offline")
        self.assertIsNone(fetch_avatar("o", failing))


class FaviconIcon(unittest.TestCase):
    def test_avatar_is_inlined_under_the_dashboard_badge(self):
        svg = Favicon("octocat", fetch=lambda owner: ("image/png", PNG)).svg().decode()
        self.assertIn("data:image/png;base64," + base64.b64encode(PNG).decode(), svg)
        self.assertTrue(svg.endswith(BADGE + "</svg>"))
        self.assertNotIn("https://", svg)

    def test_fallback_is_the_first_letter_on_a_stable_owner_color(self):
        svg = Favicon("octocat", fetch=lambda owner: None).svg().decode()
        self.assertIn(">O</text>", svg)
        self.assertIn(owner_color("octocat"), svg)
        self.assertIn(BADGE, svg)
        self.assertEqual(owner_color("OctoCat"), owner_color("octocat"))
        self.assertNotEqual(owner_color("octocat"), owner_color("hubot"))

    def test_caches_an_avatar_and_retries_a_failure_only_after_the_window(self):
        now = [0.0]
        results = [None, ("image/png", PNG)]
        calls = []

        def fetch(owner):
            calls.append(owner)
            return results.pop(0)
        icon = Favicon("octocat", fetch=fetch, monotonic=lambda: now[0])
        self.assertIn(b">O</text>", icon.svg())
        now[0] = RETRY_SECONDS - 1
        self.assertIn(b">O</text>", icon.svg())
        self.assertEqual(len(calls), 1)
        now[0] = RETRY_SECONDS
        self.assertIn(b"data:image/png", icon.svg())
        now[0] = RETRY_SECONDS * 10
        icon.svg()
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
