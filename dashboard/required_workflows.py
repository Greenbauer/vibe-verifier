"""Where a bot workflow's runs are listed, including workflows an organization ruleset requires.

A required workflow is not in the repository's own workflow list, and its by-file runs endpoint
answers 404. GitHub lists its runs under a per-repository id instead, which each run's
`workflow_url` carries (`.../actions/required_workflows/<id>`).
"""

from __future__ import annotations

import re
import threading
from urllib.parse import quote

REQUIRED_WORKFLOW_URL = re.compile(r"/actions/required_workflows/(\d+)$")


def workflow_runs(repository: str, workflow: str) -> str:
    return "repos/%s/actions/workflows/%s/runs" % (repository, quote(workflow, safe=""))


class RequiredWorkflows:
    """Repository -> workflow file name -> required workflow id, learned from run rows."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ids: dict[str, dict[str, int]] = {}
        self._searched: set[str] = set()

    def clear(self) -> None:
        with self._lock:
            self._ids.clear()
            self._searched.clear()

    def learn(self, repository: str, rows: list[dict]) -> None:
        for row in rows:
            match = REQUIRED_WORKFLOW_URL.search(str(row.get("workflow_url") or ""))
            if match and isinstance(row.get("path"), str):
                with self._lock:
                    self._ids.setdefault(repository, {})[row["path"].rsplit("/", 1)[-1]] = int(match.group(1))

    def search(self, api, repository: str, wanted: set[str], listed: set[str]) -> None:
        """Open pulls' runs teach an id for free. For a repository whose configured workflows are
        still unaccounted for, one page of its newest runs is read once, and again only after
        an id is forgotten."""
        if repository in self._searched or wanted <= listed | set(self._ids.get(repository, {})):
            return
        pages = api.page_items("repos/%s/actions/runs?per_page=100" % repository, "workflow_runs")
        self.learn(repository, next(iter(pages), ([], False))[0])
        with self._lock:
            self._searched.add(repository)

    def runs(self, repository: str, workflow: str, listed: set[str]) -> str | None:
        """Where the workflow's runs are listed, or None when the repository does not run it."""
        if workflow in listed:
            return workflow_runs(repository, workflow)
        required = self._ids.get(repository, {}).get(workflow)
        return "repos/%s/actions/required_workflows/%s/runs" % (repository, required) if required else None

    def forget(self, repository: str, workflow: str, error_code: str, base: str) -> bool:
        """A 404 while scanning a required workflow is not lost access, which would clear every
        cache: its id may have gone stale when the ruleset was replaced. Forget it so the next
        refresh looks it up again. True when the error was handled this way."""
        if "/required_workflows/" not in base or error_code != "not_found":
            return False
        with self._lock:
            self._ids.get(repository, {}).pop(workflow, None)
            self._searched.discard(repository)
        return True
