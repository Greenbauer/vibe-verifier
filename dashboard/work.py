"""Read-only work queues: plans and calls a session could pick up, and what waits on a person.

Each configured source is reached over HTTPS with a bearer read from a local file. Two kinds:

- ``plans``: a read API. ``GET <endpoint>/plans?limit=100`` answers ``{"plans": [...]}`` (rows with
  ``planId``, ``planLabel``, ``status``, ``claimedBy``, ``blockedReason``, ...) and
  ``GET <endpoint>/plans/<planId>`` the row plus ``planMarkdown`` and every amendment.
- ``calls``: an MCP server. ``list_open_calls`` (rows with ``id``, ``label``, ``status``,
  ``submitter``, ``claim``, ...) and ``get_call`` (the row plus ``brief``, ``followups``,
  ``designRefs`` and ``asks``).

Only those reads are ever made. A source that fails keeps its last items, marked stale with
the failure's code; a source never read shows the code and no items, not an empty queue.
"""

from __future__ import annotations

import json
import os
import stat
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

from .config import Config, WorkSource
from .util import iso_time, parse_time

REFRESH_SECONDS = 300
STALE_AFTER = timedelta(seconds=2 * REFRESH_SECONDS)
TIMEOUT_SECONDS = 20
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_ITEMS = 100
MAX_BODY = 20_000
MAX_UPDATE = 10_000
MAX_UPDATES = 100
MAX_TOKEN_BYTES = 4096
DONE = {"merged", "closed"}
QUEUED = {"submitted", "backlog"}
PROTOCOL_VERSION = "2025-06-18"


class WorkError(Exception):
    """A source could not be read; ``code`` is what the page shows."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code


def read_token(path: Path, uid: int | None) -> str:
    """The bearer in ``path``: a regular file of this account that no one else can read."""
    try:
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        raise WorkError("credential_missing") from None
    except OSError:
        raise WorkError("credential_unreadable") from None
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
            or (uid is not None and info.st_uid != uid) or info.st_mode & 0o077
            or info.st_size > MAX_TOKEN_BYTES):
        raise WorkError("credential_unsafe")
    try:
        token = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        raise WorkError("credential_unreadable") from None
    if not token or any(ord(char) < 33 or ord(char) > 126 for char in token):
        raise WorkError("credential_unsafe")
    return token


def _rpc_message(raw: str, request_id: int) -> dict:
    """The JSON-RPC reply with ``request_id`` from a JSON body or a text/event-stream body."""
    candidates = []
    stripped = raw.strip()
    if stripped.startswith("{"):
        candidates.append(stripped)
    else:
        data: list[str] = []
        for line in raw.splitlines() + [""]:
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
            elif not line.strip() and data:
                candidates.append("\n".join(data))
                data = []
    for candidate in candidates:
        try:
            message = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(message, dict) and message.get("id") == request_id:
            return message
    raise WorkError("invalid_response", "no reply to request %d" % request_id)


def _fetch(opener, request: urllib.request.Request) -> tuple[str, str | None]:
    """The body and MCP session header of one HTTPS exchange, or a WorkError with the page's code."""
    try:
        with opener(request, timeout=TIMEOUT_SECONDS) as response:
            session = response.headers.get("Mcp-Session-Id")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        error.close()
        code = "rejected" if error.code in (401, 403) else "not_found" if error.code == 404 else "unavailable"
        raise WorkError(code, "HTTP %d" % error.code) from None
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        raise WorkError("unreachable", str(error)) from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise WorkError("invalid_response", "reply too large")
    return raw.decode("utf-8", "replace"), session


class RestClient:
    """GETs JSON from a read API under ``endpoint`` with a bearer."""

    def __init__(self, endpoint: str, token: str, opener=None):
        self.endpoint, self.token = endpoint.rstrip("/"), token
        self.opener = opener or urllib.request.urlopen

    def get(self, path: str) -> object:
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json"}
        raw, _ = _fetch(self.opener, urllib.request.Request(self.endpoint + path, None, headers, method="GET"))
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise WorkError("invalid_response", "%s did not return JSON" % path) from None


class McpClient:
    """A minimal MCP client over streamable HTTP: initialize once, then call tools."""

    def __init__(self, endpoint: str, token: str, opener=None):
        self.endpoint, self.token = endpoint, token
        self.opener = opener or urllib.request.urlopen
        self.session: str | None = None
        self._next_id = 0

    def _post(self, method: str, params: dict) -> dict:
        self._next_id += 1
        request_id = self._next_id
        headers = {"Authorization": "Bearer " + self.token, "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        body = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}).encode()
        raw, session = _fetch(self.opener, urllib.request.Request(self.endpoint, body, headers, method="POST"))
        if session and not self.session:
            self.session = session
        message = _rpc_message(raw, request_id)
        if "error" in message or not isinstance(message.get("result"), dict):
            raise WorkError("tool_error", str((message.get("error") or {}).get("message", "no result"))[:200])
        return message["result"]

    def call(self, tool: str, arguments: dict) -> object:
        if self._next_id == 0:
            self._post("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                      "clientInfo": {"name": "vibe-dashboard", "version": "1"}})
        result = self._post("tools/call", {"name": tool, "arguments": arguments})
        text = "".join(part.get("text", "") for part in result.get("content", [])
                       if isinstance(part, dict) and part.get("type") == "text")
        if result.get("isError"):
            raise WorkError("tool_error", text[:200])
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise WorkError("invalid_response", "%s did not return JSON" % tool) from None


def _text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    return value if len(value) <= limit else value[:limit] + "\n… (cut at %d characters)" % limit


def _time(value: object) -> str | None:
    parsed = parse_time(value)
    return iso_time(parsed) if parsed else None


def _https(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    return value if parsed.scheme == "https" and parsed.hostname and parsed.username is None else None


def _number(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _item_id(value: object) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= 100 else None


def waiting(item: dict) -> dict | None:
    """What a person must do before this item moves, or None. Structural fields only."""
    open_asks = [ask for ask in item["asks"] if ask["open"]]
    if open_asks:
        return {"who": "requester", "reason": "Ask awaiting a ruling: %s" % open_asks[-1]["question"][:200]}
    status = item["status"]
    if status == "blocked":
        reason = item.get("blocked_reason")
        return {"who": "requester", "reason": "Blocked: %s" % reason if reason else "Blocked, with no reason recorded"}
    if status == "await_answer":
        return {"who": "requester", "reason": "Waiting on an answer"}
    if status == "pr_ready":
        number = item.get("pr_number")
        return {"who": "operator", "reason": "PR #%d waits on review and merge" % number if number
                else "Ready, waiting on review and merge"}
    return None


def phase(status: str) -> str:
    if status in DONE:
        return "done"
    if status in QUEUED:
        return "queued"
    return "active"


def _finish(item: dict, source: WorkSource, path: str) -> dict:
    if source.repository and item.get("pr_number"):
        item["pr_url"] = "https://github.com/%s/pull/%d" % (source.repository, item["pr_number"])
    else:
        item["pr_url"] = None
    item["item_url"] = "%s/%s/%s" % (source.app_origin, path, quote(item["id"], safe="")) if source.app_origin else None
    item["phase"] = phase(item["status"])
    item["waiting"] = None if item["phase"] == "done" else waiting(item)
    return item


def _base(row: dict, id_key: str, label_key: str) -> dict | None:
    identity = _item_id(row.get(id_key))
    status = row.get("status")
    if identity is None or not isinstance(status, str) or not status:
        return None
    return {
        "id": identity, "label": _text(row.get(label_key), 40) or identity[:8],
        "title": _text(row.get("title"), 200) or "(untitled)", "status": status[:40],
        "pr_number": _number(row.get("prNumber")), "preview_url": _https(row.get("previewUrl")),
        "created_at": _time(row.get("createdAt")), "updated_at": _time(row.get("updatedAt")),
        "body": None, "updates": [], "asks": [], "refs": [], "detail": False,
    }


def _rows(value: object) -> list[dict]:
    """The objects in a list a source returned; anything else reads as none."""
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _refs(rows: object) -> list[str]:
    return [_text(ref.get("label"), 120) or _text(ref.get("kind"), 40) or "design reference" for ref in _rows(rows)]


def _updates(rows: object) -> list[dict]:
    return [{"body": _text(update.get("body"), MAX_UPDATE) or "", "created_at": _time(update.get("createdAt")),
             "refs": _refs(update.get("designRefs"))} for update in _rows(rows)[:MAX_UPDATES]]


def _asks(rows: object) -> list[dict]:
    asks = []
    for ask in _rows(rows):
        ruling = ask.get("ruling") if isinstance(ask.get("ruling"), dict) else None
        asks.append({"question": _text(ask.get("body"), MAX_UPDATE) or "", "raised_at": _time(ask.get("raisedAt")),
                     "ruling": _text((ruling or {}).get("text"), MAX_UPDATE),
                     "ruled_at": _time((ruling or {}).get("answeredAt")), "open": ruling is None})
    return asks


def _plan_detail(client: RestClient, plan_id: str) -> dict | None:
    """The plan's own read, or None when it was removed between the list and the read."""
    try:
        detail = client.get("/plans/" + quote(plan_id, safe=""))
    except WorkError as error:
        if error.code != "not_found":
            raise
        return None
    return detail if isinstance(detail, dict) else None


def _plan(client: RestClient, source: WorkSource, row: dict) -> dict | None:
    item = _base(row, "planId", "planLabel")
    if item is None or row.get("removed") is True:
        return None
    detail = None if item["status"] in DONE else _plan_detail(client, item["id"])
    if detail is not None:
        row = {**row, **detail}
        item.update(status=str(row.get("status") or item["status"])[:40], detail=True,
                    pr_number=_number(row.get("prNumber")), preview_url=_https(row.get("previewUrl")),
                    updated_at=_time(row.get("updatedAt")) or item["updated_at"],
                    body=_text(row.get("planMarkdown"), MAX_BODY), updates=_updates(row.get("amendments")))
    item.update(claimed_by=_text(row.get("claimedBy"), 120), requester=None,
                blocked_reason=_text(row.get("blockedReason"), 500), status_text=None)
    return _finish(item, source, "plans")


def collect_plans(client: RestClient, source: WorkSource) -> tuple[list[dict], bool]:
    listing = client.get("/plans?limit=%d" % MAX_ITEMS)
    rows = listing.get("plans") if isinstance(listing, dict) else None
    if not isinstance(rows, list):
        raise WorkError("invalid_response", "the plan list returned no plans")
    items = [_plan(client, source, row) for row in _rows(rows)]
    return [item for item in items if item], len(rows) >= MAX_ITEMS


def _call(client: McpClient, source: WorkSource, row: dict) -> dict | None:
    item = _base(row, "id", "label")
    if item is None:
        return None
    detail = client.call("get_call", {"callId": item["id"]})
    if isinstance(detail, dict):
        row = {**row, **detail}
        item.update(status=str(row.get("status") or item["status"])[:40], detail=True,
                    body=_text(row.get("brief"), MAX_BODY), refs=_refs(row.get("designRefs")),
                    updates=_updates(row.get("followups")), asks=_asks(row.get("asks")))
    submitter = row.get("submitter") if isinstance(row.get("submitter"), dict) else {}
    # A session that blocks a call says why in its status description.
    note = _text(row.get("statusDescription"), 500)
    blocked = item["status"] == "blocked"
    item.update(requester=_text(submitter.get("name") or submitter.get("email"), 120),
                claimed_by={"mine": "this dashboard's account", "other": "another session"}.get(row.get("claim")),
                blocked_reason=note if blocked else None, status_text=None if blocked else note)
    return _finish(item, source, "calls")


def collect_calls(client: McpClient, source: WorkSource) -> tuple[list[dict], bool]:
    rows = client.call("list_open_calls", {"limit": MAX_ITEMS})
    if not isinstance(rows, list):
        raise WorkError("invalid_response", "list_open_calls returned no list")
    items = [_call(client, source, row) for row in _rows(rows)]
    return [item for item in items if item], len(rows) >= MAX_ITEMS


COLLECTORS = {"plans": (RestClient, collect_plans), "calls": (McpClient, collect_calls)}


class WorkFeed:
    """Every source's last reading, refreshed in the background at most every REFRESH_SECONDS while
    the page is being read. Nothing is kept across a restart: the queues are cheap to read again."""

    def __init__(self, config: Config, *, opener=None, monotonic=time.monotonic, wall_clock=None,
                 uid: int | None = None):
        self.sources = config.work_sources
        self.opener, self.monotonic = opener, monotonic
        self.wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self.uid = os.getuid() if uid is None and hasattr(os, "getuid") else uid
        self._lock = threading.Lock()
        self._running = False
        self._next = 0.0
        self._readings: dict[str, dict] = {}

    def _read(self, source: WorkSource) -> dict:
        try:
            transport, collect = COLLECTORS[source.kind]
            items, truncated = collect(transport(source.endpoint, read_token(source.token_file, self.uid), self.opener), source)
            return {"items": items, "truncated": truncated, "sampled_at": iso_time(self.wall_clock()), "error": None}
        except WorkError as error:
            return {"error": error.code}

    def refresh(self) -> None:
        try:
            for source in self.sources:
                fresh = self._read(source)
                with self._lock:
                    previous = self._readings.get(source.id, {})
                    # Lost access forgets what it showed, as GitHub's does; any other failure keeps it, stale.
                    keep = fresh["error"] and fresh["error"] != "rejected"
                    self._readings[source.id] = {**previous, **fresh} if keep else fresh
        finally:
            # A defect in one pass must not stop every later one.
            with self._lock:
                self._running = False
                self._next = self.monotonic() + REFRESH_SECONDS

    def snapshot(self, *, background: bool = True) -> dict:
        if not self.sources:
            return {"configured": False, "sources": []}
        with self._lock:
            start = not self._running and self.monotonic() >= self._next
            if start:
                self._running = True
        if start:
            if background:
                threading.Thread(target=self.refresh, name="dashboard-work-refresh", daemon=True).start()
            else:
                self.refresh()
        now = self.wall_clock()
        rows = []
        with self._lock:
            for source in self.sources:
                reading = self._readings.get(source.id)
                sampled = parse_time((reading or {}).get("sampled_at"))
                row = {"id": source.id, "label": source.label, "kind": source.kind,
                       "loading": reading is None, "error": (reading or {}).get("error"),
                       "sampled_at": (reading or {}).get("sampled_at"),
                       "truncated": bool((reading or {}).get("truncated")),
                       "items": json.loads(json.dumps((reading or {}).get("items") or []))}
                row["available"] = sampled is not None
                row["stale"] = sampled is not None and (bool(row["error"]) or now - sampled > STALE_AFTER)
                rows.append(row)
        return {"configured": True, "sources": rows}
