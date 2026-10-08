"""What a restart would otherwise lose: the last reading, and the caches that make a pass cheap.

Each document is one zlib-compressed JSON file in a directory of the dashboard's own account, mode
0600, written to a draft beside it and renamed over the old one. A file is used only when this
account owns it, nobody else can read or write it, it is within the size and age limits, and it was
written for this owner, repository selection and bot definition. Anything else is deleted, and the
dashboard starts empty, as it does with no directory at all. Contract: docs/dashboard-data.md.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import sys
import threading
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .util import iso_time, parse_time

VERSION = 1
# Older than this is history, not status, and a stopped dashboard's files are not honored longer.
MAX_AGE = timedelta(hours=24)
FUTURE_SKEW = timedelta(minutes=5)
# On disk, and decompressed. The second bounds what a start loads into memory at once (the web
# unit has 512 MiB). A document over either is not written; its previous file stays until it expires.
MAX_BYTES = 16 * 1024 * 1024
MAX_RAW = 64 * 1024 * 1024
OVER = "it is over the size limit"
SUFFIX, DRAFT = ".json.z", ".draft"
USAGE_DOCUMENTS = ("usage", "usage-responses")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _whole(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class StateStore:
    def __init__(self, directory: str | os.PathLike[str], config, *, clock=None):
        """Documents go in the owner's own directory under `directory`, so dashboards of different
        owners may be given the same one. Raises OSError when it cannot be created."""
        self.directory = Path(directory) / config.owner
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        scope = (config.owner, config.all_repositories, config.repositories, sorted(config.bots.items()))
        self.identity = hashlib.sha256(repr(scope).encode("utf-8")).hexdigest()
        self._lock = threading.Lock()
        self._refused: set[str] = set()
        os.makedirs(self.directory, mode=0o700, exist_ok=True)

    def _path(self, name: str, suffix: str = SUFFIX) -> Path:
        return self.directory / (name + suffix)

    @staticmethod
    def _remove(path: Path) -> None:
        try:
            os.unlink(path)
        except OSError:
            pass

    @staticmethod
    def _mine_alone(info: os.stat_result) -> bool:
        owned = not hasattr(os, "getuid") or info.st_uid == os.getuid()
        return stat.S_ISREG(info.st_mode) and owned and not info.st_mode & 0o077 and info.st_size <= MAX_BYTES

    def _read(self, path: Path) -> object | None:
        with os.fdopen(os.open(path, os.O_RDONLY | _NOFOLLOW), "rb") as handle:
            if not self._mine_alone(os.fstat(handle.fileno())):
                return None
            packed = handle.read(MAX_BYTES + 1)
        # A file that unpacks to more than the limit is cut off there, and what is left does not parse.
        document = json.loads(zlib.decompressobj().decompress(packed, MAX_RAW))
        if not isinstance(document, dict) or document.get("version") != VERSION or document.get("identity") != self.identity:
            return None
        saved = parse_time(document.get("saved_at"))
        if saved is None or not -FUTURE_SKEW <= self.clock() - saved <= MAX_AGE:
            return None
        return document.get("value")

    def load(self, name: str) -> object | None:
        """The stored value. None, after deleting the file, when it cannot be used."""
        path = self._path(name)
        with self._lock:
            try:
                value = self._read(path)
            except FileNotFoundError:
                return None
            except Exception:
                # Whatever makes a file unreadable, a start must not fail on it, now or next time.
                value = None
            if value is None:
                self._remove(path)
            return value

    def _write(self, name: str, packed: bytes) -> None:
        draft = self._path(name, DRAFT)
        descriptor = os.open(draft, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(packed)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(draft, self._path(name))

    def _texts(self, value: object):
        """The document's JSON in pieces, a list one item at a time: a cache of tens of megabytes is
        compressed as it is encoded and never held as one string."""
        head = json.dumps({"version": VERSION, "identity": self.identity, "saved_at": iso_time(self.clock())})
        yield head[:-1] + ', "value": '
        if not isinstance(value, list):
            yield json.dumps(value, separators=(",", ":")) + "}"
            return
        yield "["
        for index, item in enumerate(value):
            yield ("," if index else "") + json.dumps(item, separators=(",", ":"))
        yield "]}"

    def _pack(self, value: object) -> bytes:
        packer, chunks, raw, size = zlib.compressobj(1), [], 0, 0
        for text in self._texts(value):
            data = text.encode("utf-8")
            raw += len(data)
            chunks.append(packer.compress(data))
            size += len(chunks[-1])
            if raw > MAX_RAW or size > MAX_BYTES:
                raise ValueError(OVER)
        chunks.append(packer.flush())
        if size + len(chunks[-1]) > MAX_BYTES:
            raise ValueError(OVER)
        return b"".join(chunks)

    def save(self, name: str, value: object) -> bool:
        """Replace the document. One over a limit, or one the disk refuses, is not written, and the
        previous one stays: an older reading, with its age, is still better than none."""
        try:
            packed = self._pack(value)
            with self._lock:
                self._write(name, packed)
        except (OSError, ValueError, TypeError) as error:
            self._remove(self._path(name, DRAFT))
            self._say_once(name, (error.strerror or type(error).__name__) if isinstance(error, OSError) else error)
            return False
        self._refused.discard(name)
        return True

    def _say_once(self, name: str, reason: object) -> None:
        if name not in self._refused:
            self._refused.add(name)
            print("vibe-dashboard: not keeping %s across a restart: %s" % (name, reason), file=sys.stderr, flush=True)

    def clear(self, *names: str) -> None:
        """Delete the named documents, or every document when none is named."""
        with self._lock:
            for pattern in names or ("*",):
                for path in (*self.directory.glob(pattern + SUFFIX), *self.directory.glob(pattern + DRAFT)):
                    self._remove(path)


def open_store(directory: str | None, config) -> StateStore | None:
    """The store for a state directory. None, after saying why, when there is none to use."""
    if not directory:
        return None
    try:
        return StateStore(Path(directory).expanduser(), config)
    except OSError as error:
        print("vibe-dashboard: keeping nothing across a restart, %s cannot be used: %s" % (directory, error.strerror),
              file=sys.stderr, flush=True)
        return None


def _row_shaped(row: object) -> bool:
    return (isinstance(row, dict) and isinstance(row.get("repository"), str) and isinstance(row.get("pulls"), list)
            and all(isinstance(pull, dict) for pull in row["pulls"]))


def _reading_shaped(value: object) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get("coverage"), dict):
        return False
    rows, bots = value.get("repositories"), value.get("bots")
    roles = bots.get("roles") if isinstance(bots, dict) else None
    if not isinstance(rows, list) or not isinstance(roles, dict):
        return False
    return all(_row_shaped(row) for row in rows) and all(isinstance(role, dict) for role in roles.values())


def old_reading(value: object) -> dict | None:
    """A stored reading, marked as one this process did not make. None when it is not a reading.

    Every repository and bot in it is stale until this process reads it. A stale repository's pull
    requests are never merge-ready (head_state.withhold_stale_pulls), so no title in it is green."""
    if not _reading_shaped(value):
        return None
    try:
        # Every request, pass and beat copies the reading. One nested too deep to copy would fail
        # each of them where no one is looking, so it is not a reading.
        copy.deepcopy(value)
    except RecursionError:
        return None
    value.update(restored=True, stale=True, partial=True)
    for row in value["repositories"]:
        # A row carried into a later reading keeps this mark, so forgetting kept state finds it.
        row["restored"] = True
        if not row.get("unavailable"):
            row["stale"] = True
    for role in value["bots"]["roles"].values():
        role["stale"] = True
    return value


def collector_state(collector) -> dict:
    """The collector's caches as documents: completed job lists, and every answer GitHub may confirm
    unchanged. Empty for a collector that keeps neither."""
    if not hasattr(collector, "_completed_jobs"):
        return {}
    with collector._job_lock:
        jobs = [[*key, iso_time(expires), rows] for key, (expires, rows) in collector._completed_jobs.items()]
    return {"jobs": jobs, "responses": collector.api.export_cache()}


def _job_entry(row: object) -> tuple | None:
    if not isinstance(row, list) or len(row) != 5:
        return None
    repository, run_id, attempt, expires, rows = row
    when = parse_time(expires)
    known = isinstance(repository, str) and _whole(run_id) and _whole(attempt) and when is not None
    if not known or not isinstance(rows, list) or not all(isinstance(job, dict) for job in rows):
        return None
    return (repository, run_id, attempt), (when, rows)


def restore_collector(collector, store: StateStore) -> bool:
    """Give the collector back what collector_state wrote. True when anything came back."""
    if not hasattr(collector, "_completed_jobs"):
        return False
    jobs, responses = store.load("jobs"), store.load("responses")
    found = dict(filter(None, map(_job_entry, jobs if isinstance(jobs, list) else [])))
    with collector._job_lock:
        collector._completed_jobs.update(found)
    return bool(collector.api.import_cache(responses)) or bool(found)


def usage_state(reader, result: dict) -> dict:
    """The usage reader's documents: the result the page shows with every record read, and its answers."""
    records = [[repository, artifact, record] for (repository, artifact), record in reader.cache.items()]
    export = getattr(reader.api, "export_cache", None)
    return dict(zip(USAGE_DOCUMENTS, ({"result": result, "records": records}, export() if export else [])))


def _record_entry(row: object) -> tuple | None:
    if isinstance(row, list) and len(row) == 3 and isinstance(row[0], str) and _whole(row[1]) and isinstance(row[2], dict):
        return (row[0], row[1]), row[2]
    return None


def restore_usage(reader, store: StateStore) -> dict | None:
    """Give the reader back its records and answers. Returns the result the page last showed, if any."""
    kept = store.load(USAGE_DOCUMENTS[0])
    kept = kept if isinstance(kept, dict) else {}
    records = kept.get("records")
    reader.cache.update(filter(None, map(_record_entry, records if isinstance(records, list) else [])))
    take = getattr(reader.api, "import_cache", None)
    if take:
        take(store.load(USAGE_DOCUMENTS[1]))
    result = kept.get("result")
    shaped = isinstance(result, dict) and all(isinstance(result.get(key), list) for key in ("accounts", "samples"))
    return result if shaped else None
