"""Hardened loopback HTTP server for the read-only dashboard."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .config import Config
from .favicon import Favicon
from .service import DashboardService
from .live_service import LiveService

STATIC = Path(__file__).with_name("static")
FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/assets/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/assets/helpers.js": ("helpers.js", "text/javascript; charset=utf-8"),
    "/assets/charts.js": ("charts.js", "text/javascript; charset=utf-8"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def safe_json(value: object) -> bytes:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return text.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e").encode("utf-8")


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], service: DashboardService, proxy_origin: str | None,
                 favicon: Favicon):
        self.service = service
        self.proxy_origin = proxy_origin
        self.favicon = favicon
        super().__init__(address, DashboardHandler)


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def _headers(self, status: int, content_type: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.end_headers()

    def _deny(self, status: int, message: str) -> None:
        body = safe_json({"error": message})
        self._headers(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def _trusted_request(self) -> bool:
        port = self.server.server_port
        allowed_hosts = {"127.0.0.1:%s" % port, "localhost:%s" % port}
        hosts = self.headers.get_all("Host", [])
        origins = self.headers.get_all("Origin", [])
        forwarded_hosts = self.headers.get_all("X-Forwarded-Host", [])
        forwarded_protocols = self.headers.get_all("X-Forwarded-Proto", [])
        if len(hosts) > 1 or len(origins) > 1 or len(forwarded_hosts) > 1 or len(forwarded_protocols) > 1:
            self._deny(400, "duplicate routing header")
            return False
        if not hosts:
            self._deny(421, "untrusted host")
            return False
        host = hosts[0].lower()
        proxy_origin = self.server.proxy_origin
        has_forwarded = bool(forwarded_hosts or forwarded_protocols)
        if proxy_origin is None and has_forwarded:
            self._deny(403, "forwarded headers are not accepted")
            return False
        if proxy_origin is not None and has_forwarded:
            authority = urlsplit(proxy_origin).netloc
            if len(forwarded_hosts) != 1 or len(forwarded_protocols) != 1:
                self._deny(403, "incomplete forwarded origin")
                return False
            # A Host-preserving proxy (Tailscale serve, a default nginx proxy_pass) sends the
            # public authority as Host; a rewriting proxy sends a loopback Host. Accept both.
            if host not in allowed_hosts | {"localhost", authority}:
                self._deny(421, "untrusted host")
                return False
            if forwarded_hosts[0] != authority or forwarded_protocols[0] != "https":
                self._deny(403, "untrusted forwarded origin")
                return False
            if origins and origins[0] != proxy_origin:
                self._deny(403, "untrusted origin")
                return False
            return True
        if host not in allowed_hosts:
            self._deny(421, "untrusted host")
            return False
        if origins and origins[0].lower() not in {"http://" + item for item in allowed_hosts}:
            self._deny(403, "untrusted origin")
            return False
        return True

    def _safe_path(self) -> str | None:
        parsed = urlsplit(self.path)
        decoded = unquote(parsed.path)
        if parsed.query or parsed.fragment or "\\" in decoded or any(part == ".." for part in decoded.split("/")):
            return None
        return decoded

    def do_GET(self) -> None:
        if not self._trusted_request():
            return
        path = self._safe_path()
        if path is None:
            self._deny(400, "invalid path")
            return
        if path == "/api/dashboard":
            try:
                body = safe_json(self.server.service.snapshot())
            except Exception:
                self._deny(503, "dashboard source unavailable")
                return
            self._headers(200, "application/json; charset=utf-8", len(body))
            self.wfile.write(body)
            return
        if path == "/favicon.svg":
            body = self.server.favicon.svg()
            self._headers(200, "image/svg+xml", len(body))
            self.wfile.write(body)
            return
        item = FILES.get(path)
        if item is None:
            self._deny(404, "not found")
            return
        filename, content_type = item
        try:
            body = (STATIC / filename).read_bytes()
        except OSError:
            self._deny(503, "dashboard asset unavailable")
            return
        self._headers(200, content_type, len(body))
        self.wfile.write(body)

    def _read_only(self) -> None:
        if not self._trusted_request():
            return
        self._deny(405, "read-only server")

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = _read_only


def make_server(config: Config, port: int, service: DashboardService | None = None) -> DashboardServer:
    service = service or LiveService(config)
    start = getattr(service, "start_head_beat", None)
    if start:
        start()
    return DashboardServer(("127.0.0.1", port), service, config.proxy_origin, Favicon(config.owner))
