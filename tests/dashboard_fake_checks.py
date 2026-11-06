"""A head whose checks GitHub's pull request page reads differently from a naive listing.

On top of tests/dashboard_fake_github.py: CI ran twice on the head (the older run failed), the
external check has a queued rerun with no timestamps yet, a ruleset requires `lint` and classic
branch protection requires `build`, neither of which ever reports. `page()` is what GitHub's pull
request page lists for that head, the truth the dashboard's row must match."""

from __future__ import annotations

from datetime import timedelta

from dashboard_fake_github import NOW, FakeGitHub


class CheckedHead(FakeGitHub):
    def __init__(self, repo=None, *, required=("test", "lint"), protected=("build",), **options):
        self.required, self.protected = tuple(required), tuple(protected)
        super().__init__(*([repo] if repo else []), **options)

    def runs(self) -> list[dict]:
        older = self.run(10, "ci.yml", self.sha, NOW - timedelta(minutes=50), conclusion="failure", suite=5)
        return [older, *super().runs()]

    def _all_checks(self) -> list[dict]:
        queued = {**self.check_run(94, "External analysis", 8, app={"id": 40, "name": "Example checks"}),
                  "status": "queued", "conclusion": None, "started_at": None, "completed_at": None}
        return [self.check_run(90, "test", 5, conclusion="failure", minutes=49), *super().check_runs({}), queued]

    def check_runs(self, query: dict) -> list[dict]:
        rows = self._all_checks()
        if query.get("filter", "latest") == "all":
            return rows
        # GitHub's default, filter=latest: the newest completed_at of each check in a suite, so a queued
        # rerun with no completed_at yet is dropped in favour of the run it replaces.
        latest: dict = {}
        for row in rows:
            key = (row["check_suite"]["id"], row["name"])
            if key not in latest or (row["completed_at"] or "") > (latest[key]["completed_at"] or ""):
                latest[key] = row
        return list(latest.values())

    def rules(self) -> list[dict]:
        rules = super().rules()
        rules[0]["parameters"]["required_status_checks"] = [{"context": name} for name in self.required]
        return rules

    def protection(self):
        if not self.protected:
            return super().protection()
        checks = {"strict": False, "contexts": list(self.protected),
                  "checks": [{"context": name, "app_id": None} for name in self.protected]}
        return 200, {"required_status_checks": checks, "enforce_admins": {"enabled": False}}

    def page(self) -> list[tuple[str, str]]:
        """GitHub's list for the head: the newest run of each check (by app and name), then every
        required check nothing reported, as Expected."""
        newest: dict = {}
        for row in self._all_checks():
            key = (row["app"]["id"], row["name"])
            if key not in newest or row["id"] > newest[key]["id"]:
                newest[key] = row
        shown = [(row["name"], row["conclusion"] or row["status"]) for row in newest.values()]
        names = {name for name, _ in shown}
        expected = [(name, "expected") for name in dict.fromkeys(self.required + self.protected) if name not in names]
        return sorted(shown + expected)
