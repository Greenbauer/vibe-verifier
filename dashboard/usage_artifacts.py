"""Read numeric-only usage artifacts, with run provenance and bounded memory caching."""
from __future__ import annotations

import io
import json
import re
import subprocess
import time
import zipfile
from datetime import datetime, timedelta, timezone

from .gh_api import ApiError, GitHubAPI
from .util import parse_time, iso_time

MAX_ARCHIVE = 65536
MAX_RECORD = 16384
MAX_PAGES = 5  # 500 artifacts a week per repository
ROLE = {"swe-reviewer": "reviewer", "qae-explorer": "explorer"}
NAME = re.compile(r"vv-usage-(swe-reviewer|qae-explorer)-([1-9][0-9]*)\Z")
ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,63}\Z")
COUNTS = {"input_tokens", "cached_input_tokens", "cache_creation_input_tokens", "output_tokens", "reasoning_output_tokens"}


def count(value, nullable=False):
    if value is None and nullable:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10**15:
        raise ValueError("invalid count")
    return value


def decode_archive(data: bytes) -> dict:
    if len(data) > MAX_ARCHIVE:
        raise ValueError("archive too large")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) != 1 or entries[0].filename != "usage.json" or entries[0].file_size > MAX_RECORD:
            raise ValueError("unexpected archive")
        value = json.loads(archive.read(entries[0]))
    if not isinstance(value, dict):
        raise ValueError("invalid record")
    return value


def normalize(value, artifact, run, repository, now):
    """Trust only fixed structural fields that agree with the source GitHub run."""
    match = NAME.fullmatch(artifact.get("name", ""))
    source = artifact.get("workflow_run") or {}
    owner = repository.split("/", 1)[0]
    if not match or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ValueError("invalid identity")
    role, attempt = match.group(1), int(match.group(2))
    if (str(value.get("owner", "")).lower() != owner.lower() or
            str(value.get("repository", "")).lower() != repository.lower() or
            (run.get("repository") or {}).get("full_name", "").lower() != repository.lower() or
            value.get("run_id") != source.get("id") or value.get("run_id") != run.get("id") or
            value.get("run_attempt") != attempt or run.get("run_attempt") != attempt or
            value.get("head_sha") != source.get("head_sha") or value.get("head_sha") != run.get("head_sha") or
            value.get("role") != role):
        raise ValueError("source mismatch")
    observed = parse_time(value.get("observed_at"))
    if observed is None or not now - timedelta(days=7) <= observed <= now + timedelta(minutes=5):
        raise ValueError("expired sample")
    provider, status = value.get("provider"), value.get("status")
    if provider not in ("openai", "anthropic") or status not in ("complete", "partial", "unavailable"):
        raise ValueError("invalid source")
    alias = value.get("account_alias")
    if alias is not None and (not isinstance(alias, str) or not ALIAS.fullmatch(alias)):
        raise ValueError("invalid alias")
    usage = value.get("usage")
    if status == "unavailable":
        if usage is not None:
            raise ValueError("unavailable with counts")
        return None
    if not isinstance(usage, dict) or set(usage) != COUNTS:
        raise ValueError("invalid usage")
    numbers = {key: count(usage[key], key not in ("input_tokens", "output_tokens")) for key in COUNTS}
    total_input = numbers["input_tokens"]
    if provider == "anthropic":
        total_input += numbers["cached_input_tokens"] + numbers["cache_creation_input_tokens"]
    # Explicit aliases may combine runs. Unmapped runs remain separate accounts.
    account = provider + ":" + (alias or "%s:%s:%s" % (repository, run["id"], attempt))
    label = alias or "%s #%s (account unmapped)" % (repository.split("/", 1)[1], run["id"])
    return {"account": {"id": account, "label": label, "provider": provider, "quota_windows": []},
            "sample": {"account": account, "bot": ROLE[role], "timestamp": iso_time(observed),
                       "input_tokens": total_input, "output_tokens": numbers["output_tokens"], "partial": status == "partial"}}


def download(repository, artifact_id):
    """Read a fixed API endpoint, never execute/extract uploaded content."""
    command = ["gh", "api", "repos/%s/actions/artifacts/%s/zip" % (repository, artifact_id)]
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        try:
            # A GitHub usage artifact is at most 64KiB. The metadata is checked first.
            data, _ = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise ApiError("unavailable") from None
        if process.returncode or len(data) > MAX_ARCHIVE:
            raise ApiError("invalid_response")
        return data


class UsageArtifacts:
    def __init__(self, config, api=None, downloader=download, clock=time.monotonic):
        self.config = config
        self.api = api or GitHubAPI(max_calls=80)
        self.downloader, self.clock = downloader, clock
        self.cache, self.result, self.updated = {}, None, float("-inf")

    def _recent(self, repository, cutoff):
        """A repository's artifacts, page by page until one reaches past the cutoff.

        The listing runs newest first (by id, which follows creation), so once a page holds an artifact
        older than the cutoff, later pages hold nothing newer. Returns the artifacts and whether the
        listing reached that point within MAX_PAGES.
        """
        artifacts = []
        for number in range(1, MAX_PAGES + 1):
            page = self.api.one("repos/%s/actions/artifacts?per_page=100&page=%s" % (repository, number))
            rows = page.get("artifacts", [])
            artifacts.extend(rows)
            created = [parse_time(row.get("created_at")) for row in rows]
            if len(rows) < 100 or any(time is not None and time < cutoff for time in created):
                return artifacts, True
        return artifacts, False

    def collect(self, now=None):
        now = now or datetime.now(timezone.utc)
        if self.result is not None and self.clock() - self.updated < 300:
            return self.result
        self.api.begin()
        cutoff = now - timedelta(days=7)
        records, partial, seen, listed = [], False, set(), set()
        try:
            for repository in self.config.repositories:
                artifacts, complete = self._recent(repository, cutoff)
                partial |= not complete
                for artifact in artifacts:
                    matched = NAME.fullmatch(artifact.get("name", ""))
                    created = parse_time(artifact.get("created_at"))
                    if (not matched or artifact.get("expired") or created is None or created < cutoff):
                        continue
                    artifact_id = artifact.get("id")
                    size = artifact.get("size_in_bytes")
                    if (not isinstance(artifact_id, int) or not isinstance(size, int) or not 0 < size <= MAX_ARCHIVE):
                        partial = True
                        continue
                    key = (repository, artifact_id)
                    seen.add(key)
                    if key in self.cache:
                        records.append(self.cache[key])
                        continue
                    try:
                        source = artifact.get("workflow_run") or {}
                        run_id = source.get("id")
                        if not isinstance(run_id, int):
                            raise ValueError("missing source")
                        attempt = int(matched.group(2))
                        run = self.api.one("repos/%s/actions/runs/%s/attempts/%s" % (repository, run_id, attempt))
                        definition = self.config.bots[ROLE[matched.group(1)]]
                        if str(run.get("path", "")).split("/")[-1] != definition.workflow:
                            raise ValueError("unexpected source workflow")
                        record = normalize(decode_archive(self.downloader(repository, artifact_id)), artifact, run, repository, now)
                        self.cache[key] = record
                        records.append(record)
                    except (ApiError, ValueError, TypeError, KeyError, zipfile.BadZipFile, OSError):
                        partial = True
                listed.add(repository)
        except ApiError as error:
            partial = True
            if error.code != "request_budget_exhausted":
                # Failed access invalidates prior private usage, including cached history.
                self.cache, records, listed = {}, [], set()
        # A repository the call budget did not reach keeps what earlier passes read, inside the window;
        # the next pass reads it again. A listed repository keeps only what it still lists.
        self.cache = {key: value for key, value in self.cache.items()
                      if key in seen or (key[0] not in listed and
                                         (value is None or parse_time(value["sample"]["timestamp"]) >= cutoff))}
        records.extend(value for key, value in self.cache.items() if key not in seen)
        accounts, samples = {}, []
        for record in records:
            if record is None:
                partial = True
            if record:
                accounts[record["account"]["id"]] = record["account"]
                samples.append(record["sample"])
                partial |= record["sample"]["partial"]
        self.result = {"available": True, "sampled_at": iso_time(now), "stale": False,
                       "accounts": list(accounts.values()), "samples": samples,
                       "completeness": ("Partial capture. " if partial else "") +
                         "Observed source-run tokens only; older runs have no history. Unmapped accounts are kept separate.",
                       "partial": partial}
        self.updated = self.clock()
        return self.result
