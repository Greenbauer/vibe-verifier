"""Whether an agent may merge a pull request without a human review, by the configured merge policy.

The decision is the catalog's own (gates/_merge_grant.py, the code behind `bin/vibe-verifier
merge-grant`), the same code a merge guard runs, so the pill and the guard apply one set of rules
(a file over 1 MB hides the pill: docs/features/merge-grant.md, Gotchas). This module only feeds it: the pull request's files and their text at the base and the head, read through the
dashboard's budgeted API, and the answer kept per head, base and title, so a pull request is read once
per push. Eligibility is not merge-readiness: it says nothing of checks, reviews or conflicts.
"""

from __future__ import annotations

import importlib.util
import threading
from pathlib import Path
from urllib.parse import quote

from .gh_api import ApiError

_SOURCE = Path(__file__).resolve().parents[1] / "gates" / "_merge_grant.py"


def _engine():
    spec = importlib.util.spec_from_file_location("vibe_verifier_merge_grant", _SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ENGINE = _engine()
PolicyError = ENGINE.PolicyError


def load_policy(path: Path) -> dict:
    return ENGINE.load_policy(str(path))


class MergeGrants:
    """One answer per pull request, replaced when its head, base or title moves."""

    def __init__(self, policy: dict):
        self.policy = policy
        self._known: dict[tuple, tuple] = {}
        self._lock = threading.Lock()

    def grant(self, api, repository: str, current: dict) -> str | None:
        """The name of the grant that lets an agent merge it, or None (also when GitHub did not answer)."""
        number = current.get("number")
        fingerprint = ((current.get("head") or {}).get("sha"), (current.get("base") or {}).get("sha"), current.get("title"))
        with self._lock:
            known = self._known.get((repository, number))
        if known and known[0] == fingerprint:
            return known[1]
        try:
            name, _ = ENGINE.decide(self.policy, repository, title=lambda: str(current.get("title") or ""),
                                    files=lambda: self._files(api, repository, current),
                                    read=lambda path, side: self._read(api, repository, fingerprint, path, side))
        except ApiError:
            return None                       # a spent budget or a transient failure: ask again next pass
        with self._lock:
            self._known[(repository, number)] = (fingerprint, name)
        return name

    @staticmethod
    def _files(api, repository: str, current: dict) -> list[tuple]:
        rows = api.items("repos/%s/pulls/%s/files" % (repository, current.get("number")))
        listed = [(row.get("status"), row.get("previous_filename") or row.get("filename"), row.get("filename"))
                  for row in rows if isinstance(row.get("filename"), str)]
        count = current.get("changed_files")
        if not listed and count == 0:
            raise ENGINE.Unreadable("GitHub listed no changed files")      # as the command says of an empty PR
        if len(listed) != count:
            # Past GitHub's 3000-file listing the answer is a lasting no; below it, a short listing is a lag.
            if isinstance(count, int) and count > 3000:
                raise ENGINE.Unreadable("GitHub lists at most 3000 of its %d changed files" % count)
            raise ApiError("invalid_response")
        return listed

    @staticmethod
    def _read(api, repository: str, fingerprint: tuple, path: str, side: str) -> str | None:
        ref = fingerprint[1] if side == "base" else fingerprint[0]
        try:
            return ENGINE.contents_text(api.one("repos/%s/contents/%s?ref=%s" % (repository, quote(path), ref)))
        except ApiError as error:
            if error.code == "not_found":
                return None
            raise
