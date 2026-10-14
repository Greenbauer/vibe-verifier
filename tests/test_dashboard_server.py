"""Loopback server boundaries: routing headers, read-only methods, Host/Origin/traversal and safe JSON."""

import io
import json
import unittest
from unittest import mock

from dashboard.config import BotDefinition, Config
from dashboard.favicon import Favicon
from dashboard.server import DashboardHandler, make_server


def config():
    bots = {name: BotDefinition(name + ".yml", (name,)) for name in ("reviewer", "explorer", "verifier")}
    return Config("octocat", ("octocat/example",), bots, None)


class FakeService:
    def snapshot(self):
        return {"version": 1, "owner": "octocat", "github": {"sampled_at": None, "repositories": [],
                "bots": {"roles": {}}, "coverage": {"selected": 1},
                "title": "<img src=x onerror=alert(1)>"}, "telemetry": {"available": False}}

class FakeSocket:
    def __init__(self, request):
        self.request = io.BytesIO(request)
        self.response = bytearray()
    def makefile(self, mode, buffering=None):
        return self.request
    def sendall(self, data):
        self.response.extend(data)
    def close(self):
        pass
class FakeServer:
    server_port = 8765
    service = FakeService()
    favicon = Favicon("octocat", fetch=lambda owner: None)
    def __init__(self, proxy_origin=None):
        self.proxy_origin = proxy_origin
class ServerSecurity(unittest.TestCase):
    def request(self, method, path, headers=None, proxy_origin=None):
        if isinstance(headers, dict):
            header_items = list(headers.items())
        else:
            header_items = list(headers or ())
        if not any(name.lower() == "host" for name, _ in header_items):
            header_items.insert(0, ("Host", "127.0.0.1:8765"))
        raw = "%s %s HTTP/1.1\r\n%s\r\n\r\n" % (
            method, path, "\r\n".join("%s: %s" % item for item in header_items))
        connection = FakeSocket(raw.encode("ascii"))
        DashboardHandler(connection, ("127.0.0.1", 12345), FakeServer(proxy_origin))
        head, body = bytes(connection.response).split(b"\r\n\r\n", 1)
        lines = head.decode("ascii").split("\r\n")
        values = {name.lower(): value.strip() for name, value in
                  (line.split(":", 1) for line in lines[1:] if ":" in line)}
        return int(lines[0].split()[1]), values, body
    def test_server_binds_only_loopback_and_serves_json_without_literal_html(self):
        with mock.patch("dashboard.server.DashboardServer") as server:
            make_server(config(), 8765, FakeService())
            self.assertEqual(server.call_args.args[0], ("127.0.0.1", 8765))
        status, headers, body = self.request("GET", "/api/dashboard")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
        self.assertNotIn(b"<img", body)
        self.assertEqual(json.loads(body)["owner"], "octocat")
        self.assertNotIn("access-control-allow-origin", headers)
    def test_favicon_is_served_same_origin_and_used_as_tab_icon_and_header_logo(self):
        status, headers, body = self.request("GET", "/favicon.svg")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "image/svg+xml")
        self.assertIn("img-src 'self' data:", headers["content-security-policy"])
        self.assertIn(b">O</text>", body)
        _, _, page = self.request("GET", "/")
        self.assertIn(b'<link rel="icon" href="/favicon.svg" type="image/svg+xml">', page)
        self.assertIn(b'<img class="brand-mark" src="/favicon.svg" alt="">', page)
    def test_untrusted_host_and_origin_are_rejected(self):
        self.assertEqual(self.request("GET", "/", {"Host": "example.invalid"})[0], 421)
        self.assertEqual(self.request("GET", "/", {"Origin": "https://example.invalid"})[0], 403)
    def test_valid_proxy_request_uses_exact_forwarded_authority_and_https(self):
        origin = "https://dashboard.example.test:8443"
        for host in ("localhost", "localhost:8765", "127.0.0.1:8765"):
            with self.subTest(host=host):
                status, headers, _ = self.request("GET", "/", {
                    "Host": host,
                    "Origin": origin,
                    "X-Forwarded-Host": "dashboard.example.test:8443",
                    "X-Forwarded-Proto": "https",
                }, proxy_origin=origin)
                self.assertEqual(status, 200)
                self.assertIn("script-src 'self'", headers["content-security-policy"])
                self.assertNotIn("access-control-allow-origin", headers)
    def test_host_preserving_proxy_may_send_the_configured_authority_as_host(self):
        origin = "https://dashboard.example.test:8443"
        forwarded = {"X-Forwarded-Host": "dashboard.example.test:8443", "X-Forwarded-Proto": "https"}
        for path in ("/", "/api/dashboard"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path, {
                    "Host": "dashboard.example.test:8443", "Origin": origin, **forwarded,
                }, proxy_origin=origin)[0], 200)
        for host in ("other.example.test:8443", "dashboard.example.test", "dashboard.example.test:9999"):
            with self.subTest(host=host):
                self.assertEqual(self.request("GET", "/", {"Host": host, **forwarded},
                                              proxy_origin=origin)[0], 421)
        # The public authority is only trusted alongside complete, matching forwarded headers.
        self.assertEqual(self.request("GET", "/", {"Host": "dashboard.example.test:8443"},
                                      proxy_origin=origin)[0], 421)
        self.assertEqual(self.request("GET", "/", {
            "Host": "dashboard.example.test:8443", "X-Forwarded-Host": "other.example.test:8443",
            "X-Forwarded-Proto": "https"}, proxy_origin=origin)[0], 403)
        self.assertEqual(self.request("GET", "/", {
            "Host": "dashboard.example.test:8443", **forwarded}, proxy_origin=None)[0], 403)
    def test_default_mode_rejects_forwarded_headers(self):
        self.assertEqual(self.request("GET", "/", {
            "X-Forwarded-Host": "dashboard.example.test",
            "X-Forwarded-Proto": "https",
        })[0], 403)
    def test_proxy_mode_rejects_missing_mismatched_and_duplicate_forwarded_headers(self):
        origin = "https://dashboard.example.test"
        denied = (
            {"X-Forwarded-Host": "dashboard.example.test"},
            {"X-Forwarded-Proto": "https"},
            {"X-Forwarded-Host": "other.example.test", "X-Forwarded-Proto": "https"},
            {"X-Forwarded-Host": "dashboard.example.test", "X-Forwarded-Proto": "http"},
            [("X-Forwarded-Host", "dashboard.example.test"),
             ("X-Forwarded-Host", "dashboard.example.test"), ("X-Forwarded-Proto", "https")],
            [("X-Forwarded-Host", "dashboard.example.test"), ("X-Forwarded-Proto", "https"),
             ("X-Forwarded-Proto", "https")],
        )
        for headers in denied:
            with self.subTest(headers=headers):
                self.assertNotEqual(self.request(
                    "GET", "/", headers, proxy_origin=origin)[0], 200)
    def test_proxy_mode_names_each_refusal(self):
        origin = "https://dashboard.example.test"
        forwarded = {"X-Forwarded-Host": "dashboard.example.test", "X-Forwarded-Proto": "https"}
        cases = (
            ({"X-Forwarded-Proto": "https"}, 403, "incomplete forwarded origin"),
            ({"Host": "other.example.test", **forwarded}, 421, "untrusted host"),
            ({"X-Forwarded-Host": "dashboard.example.test", "X-Forwarded-Proto": "http"}, 403,
             "untrusted forwarded origin"),
            ({"Origin": "https://other.example.test", **forwarded}, 403, "untrusted origin"),
        )
        for headers, status, message in cases:
            with self.subTest(headers=headers):
                answer = self.request("GET", "/", headers, proxy_origin=origin)
                self.assertEqual((answer[0], json.loads(answer[2])), (status, {"error": message}))
    def test_duplicate_host_origin_and_hostile_proxy_origin_are_rejected(self):
        origin = "https://dashboard.example.test"
        self.assertEqual(self.request("GET", "/", [
            ("Host", "127.0.0.1:8765"), ("Host", "127.0.0.1:8765")])[0], 400)
        self.assertEqual(self.request("GET", "/", [
            ("Origin", "http://127.0.0.1:8765"),
            ("Origin", "http://127.0.0.1:8765")])[0], 400)
        self.assertEqual(self.request("GET", "/", {
            "Origin": "https://hostile.example.test",
            "X-Forwarded-Host": "dashboard.example.test",
            "X-Forwarded-Proto": "https",
        }, proxy_origin=origin)[0], 403)
    def test_direct_loopback_requests_still_work_when_proxy_is_configured(self):
        origin = "https://dashboard.example.test"
        local_headers = {"Host": "localhost:8765", "Origin": "http://localhost:8765"}
        self.assertEqual(self.request("GET", "/", local_headers, proxy_origin=origin)[0], 200)
        self.assertEqual(self.request(
            "GET", "/api/dashboard", local_headers, proxy_origin=origin)[0], 200)
    def test_path_traversal_is_rejected_before_static_lookup(self):
        self.assertEqual(self.request("GET", "/%2e%2e/secret")[0], 400)
        self.assertEqual(self.request("GET", "/assets/unknown.js")[0], 404)
    def test_every_mutating_method_is_denied(self):
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            with self.subTest(method=method):
                self.assertEqual(self.request(method, "/api/dashboard")[0], 405)
    def test_every_mutating_method_is_denied_through_proxy(self):
        origin = "https://dashboard.example.test"
        headers = {"Host": "localhost", "X-Forwarded-Host": "dashboard.example.test",
                   "X-Forwarded-Proto": "https"}
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            with self.subTest(method=method):
                self.assertEqual(self.request(
                    method, "/api/dashboard", headers, proxy_origin=origin)[0], 405)
    def test_static_response_has_no_remote_script_permission(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("script-src 'self'", headers["content-security-policy"])
        self.assertNotIn(b"https://", body)

if __name__ == "__main__":
    unittest.main()
