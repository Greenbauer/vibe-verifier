"""Read-only work queues: plans and calls a session could pick up, and what waits on a person.

Each configured source is an MCP server reached over HTTPS with a bearer read from a local file. Two
kinds are understood, by the tools they expose:

- ``plans``: ``list_plans`` (rows with ``planId``, ``planLabel``, ``status``, ``claimedBy``,
  ``blockedReason``, ...) and ``get_plan`` (the row plus ``planMarkdown`` and every amendment).
- ``calls``: ``list_open_calls`` (rows with ``id``, ``label``, ``status``, ``submitter``, ``claim``,
  ...) and ``get_call`` (the row plus ``brief``, ``followups``, ``designRefs`` and ``asks``).

Only those read tools are ever called. A source that fails keeps its last items, marked stale with
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
        request = urllib.request.Request(self.endpoint, body, headers, method="POST")
        try:
            with self.opener(request, timeout=TIMEOUT_SECONDS) as response:
                session = response.headers.get("Mcp-Session-Id")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            raise WorkError("rejected" if error.code in (401, 403) else "unavailable",
                            "HTTP %d" % error.code) from None
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            raise WorkError("unreachable", str(error)) from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise WorkError("invalid_response", "reply too large")
        if session and not self.session:
            self.session = session
        message = _rpc_message(raw.decode("utf-8", "replace"), request_id)
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


def todo(item: dict) -> dict | None:
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
    item["todo"] = None if item["phase"] == "done" else todo(item)
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


def collect_plans(client: McpClient, source: WorkSource) -> tuple[list[dict], bool]:
    rows = client.call("list_plans", {"limit": MAX_ITEMS})
    rows = rows.get("plans") if isinstance(rows, dict) else None
    if not isinstance(rows, list):
        raise WorkError("invalid_response", "list_plans returned no plans")
    items = []
    for row in rows:
        item = _base(row, "planId", "planLabel") if isinstance(row, dict) else None
        if item is None or row.get("removed") is True:
            continue
        if item["status"] not in DONE:
            detail = client.call("get_plan", {"planId": item["id"]})
            if isinstance(detail, dict):
                row = {**row, **detail}
                item.update(status=str(row.get("status") or item["status"])[:40], detail=True,
                            pr_number=_number(row.get("prNumber")), preview_url=_https(row.get("previewUrl")),
                            updated_at=_time(row.get("updatedAt")) or item["updated_at"],
                            body=_text(row.get("planMarkdown"), MAX_BODY))
                item["updates"] = [
                    {"body": _text(update.get("body"), MAX_UPDATE) or "", "created_at": _time(update.get("createdAt")), "refs": []}
                    for update in (row.get("amendments") or [])[:MAX_UPDATES] if isinstance(update, dict)]
        item["claimed_by"] = _text(row.get("claimedBy"), 120)
        item["requester"] = None
        item["blocked_reason"] = _text(row.get("blockedReason"), 500)
        item["status_text"] = None
        items.append(_finish(item, source, "plans"))
    return items, len(rows) >= MAX_ITEMS


def _refs(rows: object) -> list[str]:
    return [_text(ref.get("label"), 120) or _text(ref.get("kind"), 40) or "design reference"
            for ref in (rows if isinstance(rows, list) else []) if isinstance(ref, dict)]


def collect_calls(client: McpClient, source: WorkSource) -> tuple[list[dict], bool]:
    rows = client.call("list_open_calls", {"limit": MAX_ITEMS})
    if not isinstance(rows, list):
        raise WorkError("invalid_response", "list_open_calls returned no list")
    items = []
    for row in rows:
        item = _base(row, "id", "label") if isinstance(row, dict) else None
        if item is None:
            continue
        detail = client.call("get_call", {"callId": item["id"]})
        if isinstance(detail, dict):
            row = {**row, **detail}
            item.update(status=str(row.get("status") or item["status"])[:40], detail=True,
                        body=_text(row.get("brief"), MAX_BODY), refs=_refs(row.get("designRefs")))
            item["updates"] = [
                {"body": _text(update.get("body"), MAX_UPDATE) or "", "created_at": _time(update.get("createdAt")),
                 "refs": _refs(update.get("designRefs"))}
                for update in (row.get("followups") or [])[:MAX_UPDATES] if isinstance(update, dict)]
            item["asks"] = [
                {"question": _text(ask.get("body"), MAX_UPDATE) or "", "raised_at": _time(ask.get("raisedAt")),
                 "ruling": _text((ask.get("ruling") or {}).get("text"), MAX_UPDATE),
                 "ruled_at": _time((ask.get("ruling") or {}).get("answeredAt")),
                 "open": not isinstance(ask.get("ruling"), dict)}
                for ask in (row.get("asks") or []) if isinstance(ask, dict)]
        submitter = row.get("submitter") if isinstance(row.get("submitter"), dict) else {}
        item["requester"] = _text(submitter.get("name") or submitter.get("email"), 120)
        item["claimed_by"] = {"mine": "this dashboard's account", "other": "another session"}.get(row.get("claim"))
        item["blocked_reason"] = None
        item["status_text"] = _text(row.get("statusDescription"), 500)
        items.append(_finish(item, source, "calls"))
    return items, len(rows) >= MAX_ITEMS


COLLECTORS = {"plans": collect_plans, "calls": collect_calls}


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
            client = McpClient(source.endpoint, read_token(source.token_file, self.uid), self.opener)
            items, truncated = COLLECTORS[source.kind](client, source)
            return {"items": items, "truncated": truncated, "sampled_at": iso_time(self.wall_clock()), "error": None}
        except WorkError as error:
            return {"error": error.code}

    def refresh(self) -> None:
        for source in self.sources:
            fresh = self._read(source)
            with self._lock:
                previous = self._readings.get(source.id, {})
                # Lost access forgets what it showed, as GitHub's does; any other failure keeps it, stale.
                keep = fresh["error"] and fresh["error"] != "rejected"
                self._readings[source.id] = {**previous, **fresh} if keep else fresh
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
