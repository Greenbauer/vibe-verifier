"""Read numeric-only usage artifacts, with run provenance and bounded memory caching."""
from __future__ import annotations

import io
import json
import re
import subprocess
import time
import zipfile
from datetime import datetime, timedelta, timezone

from .bot_runs import qae_instance
from .gh_api import PRIVATE_FAILURES, ApiError, GitHubAPI
from .util import parse_time, iso_time

OBSERVED = "Observed source-run tokens only; older runs have no history. Unmapped accounts are kept separate."
CAPTURE = "Partial capture. "

MAX_ARCHIVE = 65536
MAX_RECORD = 16384
MAX_PAGES = 5  # 500 artifacts a week per repository
ROLE = {"swe-reviewer": "reviewer", "qae-explorer": "explorer"}
# `vv-usage-<role>-<run attempt>`, then `-<explorer>` for a QAE explorer past the first of a split run.
NAME = re.compile(r"vv-usage-(swe-reviewer|qae-explorer)-([1-9][0-9]*)(?:-([1-9][0-9]*))?\Z")
ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,63}\Z")
COUNTS = {"input_tokens", "cached_input_tokens", "cache_creation_input_tokens", "output_tokens", "reasoning_output_tokens"}


def explorer_jobs(jobs, explorer):
    """The job names of the explorer an artifact is from: the bot's own for the first, and for one past
    the first of a split run the name GitHub gives that matrix job, `<job> (<n>)`."""
    return tuple("%s (%s)" % (job, explorer) for job in jobs) if explorer else jobs


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


def _gap_inside(result, window_start):
    """A capture with no counts makes the history partial only when it falls inside the plan window."""
    for stamp in result.get("gaps") or []:
        when = parse_time(stamp)
        if window_start is None or when is None or when >= window_start:
            return True
    return False


def apply_plan_window(result, window_start):
    """`partial` means token history since the plan window began is incomplete.

    A listing that never reaches the seven-day cutoff is still complete when the oldest artifact it
    did read is at or before the window start (`pace.plan_window_start`). An unreadable artifact, a
    partial sample, or a budget stop stays partial either way. A capture that reports no counts
    does too, when that capture is inside the window. Results from before this field existed
    are left alone.
    """
    if not isinstance(result, dict) or "hard_partial" not in result:
        return result
    if window_start is None:
        partial = bool(result["hard_partial"]) or not result.get("listing_complete", False) or _gap_inside(result, None)
    else:
        covered = parse_time(result.get("covered_until"))
        partial = (bool(result["hard_partial"]) or _gap_inside(result, window_start) or
                   covered is None or covered > window_start)
    result["partial"] = partial
    result["completeness"] = (CAPTURE if partial else "") + OBSERVED
    return result


def _still_cached(value, cutoff):
    if value is None:
        return True
    stamp = value["sample"]["timestamp"] if "sample" in value else value.get("gap_at")
    when = parse_time(stamp) if isinstance(stamp, str) else None
    return when is not None and when >= cutoff


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
        self._names = None
        # True when the last scan met lost access: what it returned must not be kept on disk.
        self.lost_access = False

    def _budget_left(self):
        limit = getattr(self.api, "max_calls", None)
        if limit is None:
            return True
        return getattr(self.api, "calls", 0) < limit

    def _qae_instance(self, repository, run_id, attempt, job_names):
        """The explore job's lane instance, from its runner name. A miss is not a token gap."""
        if not self._budget_left():
            return None, False
        try:
            page = self.api.one("repos/%s/actions/runs/%s/attempts/%s/jobs?per_page=100" %
                                (repository, run_id, attempt))
        except ApiError:
            return None, False
        jobs = page.get("jobs") if isinstance(page, dict) else None
        if not isinstance(jobs, list):
            return None, True
        found = [qae_instance(job.get("runner_name")) for job in jobs
                 if isinstance(job, dict) and job.get("name") in job_names]
        if found and all(number == found[0] for number in found):
            return found[0], True
        return None, True

    def _recent(self, repository, cutoff):
        """A repository's artifacts, page by page until one reaches past the cutoff.

        The listing runs newest first (by id, which follows creation), so once a page holds an artifact
        older than the cutoff, later pages hold nothing newer. Returns the artifacts, whether the
        listing reached that point within MAX_PAGES, and the oldest created_at on the pages read.
        """
        artifacts, oldest = [], None
        for number in range(1, MAX_PAGES + 1):
            page = self.api.one("repos/%s/actions/artifacts?per_page=100&page=%s" % (repository, number))
            rows = page.get("artifacts", [])
            artifacts.extend(rows)
            created = [parse_time(row.get("created_at")) for row in rows]
            for time in created:
                if time is not None and (oldest is None or time < oldest):
                    oldest = time
            if len(rows) < 100 or any(time is not None and time < cutoff for time in created):
                return artifacts, True, oldest
        return artifacts, False, oldest

    def collect(self, now=None, window_start=None, repositories=None):
        now = now or datetime.now(timezone.utc)
        names = tuple(self.config.repositories if repositories is None else repositories)
        if self._names != names:
            self.result = None
            self._names = names
        if self.result is not None and self.clock() - self.updated < 300:
            return self.result
        self.api.begin()
        self.lost_access = False
        cutoff = now - timedelta(days=7)
        records, hard, seen, listed = [], False, set(), set()
        listing_complete, bounds, unproven = True, [], False
        try:
            for repository in names:
                artifacts, complete, oldest = self._recent(repository, cutoff)
                if complete:
                    bounds.append(cutoff)
                else:
                    listing_complete = False
                    if oldest is None:
                        unproven = True
                    else:
                        bounds.append(oldest)
                for artifact in artifacts:
                    matched = NAME.fullmatch(artifact.get("name", ""))
                    created = parse_time(artifact.get("created_at"))
                    if (not matched or artifact.get("expired") or created is None or created < cutoff):
                        continue
                    artifact_id = artifact.get("id")
                    size = artifact.get("size_in_bytes")
                    if (not isinstance(artifact_id, int) or not isinstance(size, int) or not 0 < size <= MAX_ARCHIVE):
                        hard = True
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
                        if record is None:
                            record = {"gap_at": artifact.get("created_at")}
                        elif ROLE[matched.group(1)] == "explorer":
                            # Filled after the token reads, so a runner lookup cannot spend the
                            # budget the history itself needs.
                            record["_lookup"] = (repository, run_id, attempt, explorer_jobs(definition.jobs, matched.group(3)))
                        self.cache[key] = record
                        records.append(record)
                    except (ApiError, ValueError, TypeError, KeyError, zipfile.BadZipFile, OSError):
                        hard = True
                listed.add(repository)
        except ApiError as error:
            hard = True
            if error.code in PRIVATE_FAILURES:
                # Failed access invalidates prior private usage, including cached history.
                self.cache, records, listed, self.lost_access = {}, [], set(), True
        # A repository this pass did not reach (a spent call budget, a rate limit, a timeout) keeps what
        # earlier passes read, inside the window; the next pass reads it again. A listed repository
        # keeps only what it still lists.
        self.cache = {key: value for key, value in self.cache.items()
                      if key in seen or (key[0] not in listed and _still_cached(value, cutoff))}
        records.extend(value for key, value in self.cache.items() if key not in seen)
        for record in records:
            lookup = record.get("_lookup") if isinstance(record, dict) else None
            if not lookup or record.get("_instance_known"):
                continue
            number, known = self._qae_instance(*lookup)
            if not known:
                break
            record["sample"]["instance"] = number
            record["_instance_known"] = True
        accounts, samples, gaps = {}, [], []
        for record in records:
            if not record:
                hard = True
                continue
            if "sample" not in record:
                gaps.append(record.get("gap_at"))
                continue
            accounts[record["account"]["id"]] = record["account"]
            samples.append(record["sample"])
            hard |= record["sample"]["partial"]
        covered = None if unproven or not bounds else max(bounds)
        self.result = apply_plan_window(
            {"available": True, "sampled_at": iso_time(now), "stale": False,
             "accounts": list(accounts.values()), "samples": samples, "gaps": gaps,
             "hard_partial": hard, "listing_complete": listing_complete and not hard,
             "covered_until": iso_time(covered) if covered else None},
            window_start)
        self.updated = self.clock()
        return self.result
