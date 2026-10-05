"""Tab icon: the configured owner's GitHub avatar with the dashboard's check badge.

The avatar is fetched server-side and inlined as a data URI, so the browser makes no third-party
request and the CSP stays `img-src 'self' data:`. When the avatar cannot be read, the icon falls
back to the owner's first letter on a color derived from a hash of the owner name.
"""

from __future__ import annotations

import base64
import hashlib
import threading
import time
import urllib.request
from xml.sax.saxutils import escape

AVATAR_URL = "https://github.com/%s.png?size=64"
MAX_AVATAR_BYTES = 256 * 1024
RETRY_SECONDS = 300
IMAGE_TYPES = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"))

# The badge reuses the dashboard's --green and --bg tokens from static/styles.css.
BADGE = ('<circle cx="48" cy="48" r="15" fill="#63d297" stroke="#090909" stroke-width="4"/>'
         '<path d="M41 48.5l5 5 9-10" fill="none" stroke="#090909" stroke-width="4" '
         'stroke-linecap="round" stroke-linejoin="round"/>')


def fetch_avatar(owner: str, opener=urllib.request.urlopen) -> tuple[str, bytes] | None:
    """Return (media type, bytes) for the owner's public avatar, or None if unreadable."""
    try:
        with opener(AVATAR_URL % owner, timeout=10) as response:
            body = response.read(MAX_AVATAR_BYTES + 1)
    except (OSError, ValueError):
        return None
    if len(body) > MAX_AVATAR_BYTES:
        return None
    return next(((media, body) for magic, media in IMAGE_TYPES if body.startswith(magic)), None)


def owner_color(owner: str) -> str:
    hue = int.from_bytes(hashlib.sha256(owner.lower().encode("utf-8")).digest()[:2], "big") % 360
    return "hsl(%d, 55%%, 38%%)" % hue


def favicon_svg(owner: str, avatar: tuple[str, bytes] | None) -> str:
    if avatar:
        media, body = avatar
        base = ('<clipPath id="c"><rect width="64" height="64" rx="14"/></clipPath>'
                '<image href="data:%s;base64,%s" width="64" height="64" clip-path="url(#c)"/>'
                % (media, base64.b64encode(body).decode("ascii")))
    else:
        base = ('<rect width="64" height="64" rx="14" fill="%s"/>'
                '<text x="30" y="31" dominant-baseline="central" text-anchor="middle" '
                'font-family="system-ui, sans-serif" font-size="40" font-weight="700" '
                'fill="#ffffff">%s</text>' % (owner_color(owner), escape(owner[0].upper())))
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">%s%s</svg>'
            % (base, BADGE))


class Favicon:
    """Caches the rendered icon; retries a failed avatar read at most every RETRY_SECONDS."""

    def __init__(self, owner: str, *, fetch=fetch_avatar, monotonic=time.monotonic):
        self.owner = owner
        self.fetch = fetch
        self.monotonic = monotonic
        self._svg: bytes | None = None
        self._has_avatar = False
        self._retry_at = 0.0
        self._lock = threading.Lock()

    def svg(self) -> bytes:
        with self._lock:
            if not self._has_avatar and self.monotonic() >= self._retry_at:
                avatar = self.fetch(self.owner)
                self._has_avatar = avatar is not None
                self._retry_at = self.monotonic() + RETRY_SECONDS
                self._svg = favicon_svg(self.owner, avatar).encode("utf-8")
            return self._svg
