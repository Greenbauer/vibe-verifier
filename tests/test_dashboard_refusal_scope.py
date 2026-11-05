"""A refused or failed GitHub read costs only what that read carries.

The real collector and service run against tests/dashboard_fake_github.py, and every kind of read
the collector makes is refused in turn:

- a family whose own hourly counter is spent (GraphQL: refused with points still left) pauses only
  that family, so the next pass still reads the others;
- a 403 on a read that only adds to a pull request (branch rules, protection, ruleset history,
  GraphQL signals, bot history, the subscription files) leaves the pull request and its checks on
  the page;
- a 502 on any read leaves the last sample on the page.

The reads that make a pull request's own evidence (CORE), and the subscription files every token may
read, are exempt from the 403 rule: a 403 there is lost access, which deletes what was read before
by design. Any kind of read added later is
covered without a change here, because the families come from what the collector asked for."""

import unittest

from dashboard.config import BotDefinition, Config
from dashboard.gh_api import ApiError, GitHubAPI
from dashboard.github import GitHubCollector
from dashboard.service import DashboardService

from dashboard_fake_github import NOW, REPO, FakeGitHub

# The reads that are a pull request's own evidence: the listing, the pull, its head's checks,
# statuses, Actions runs and their jobs.
CORE = {
    "repos/*/*/pulls", "repos/*/*/pulls/*", "repos/*/*/commits/*/check-runs", "repos/*/*/commits/*/check-suites",
    "repos/*/*/check-suites/*/check-runs", "repos/*/*/commits/*/status", "repos/*/*/actions/runs",
    "repos/*/*/actions/runs/*/attempts/*/jobs", "repos/*/*/actions/runs/*/jobs",
}
# Contents read is one of the permissions every dashboard token holds (docs/dashboard-host.md), so a
# 403 on the subscription files is lost access too.
LOST_ACCESS = CORE | {"repos/*/*/contents/.vibe-verifier", "repos/*/*/contents/.github/workflows/*"}
# GitHub reports the whole token's counter here, so its refusal is a global answer.
GLOBAL = {"rate_limit"}
CHECKS = ["External analysis", "gate", "test"]


def config():
    return Config("octocat", (REPO,), {"reviewer": BotDefinition("review.yml", ("review",)),
                                       "explorer": BotDefinition("explore.yml", ("explore",))}, None)


def collector(fake):
    return GitHubCollector(config(), GitHubAPI(runner=fake, clock=lambda: NOW.timestamp()), clock=lambda: NOW)


def service(fake):
    return DashboardService(config(), collector(fake), monotonic=lambda: 0, wall_clock=lambda: NOW)


def families():
    fake = FakeGitHub()
    collector(fake).collect()
    return sorted({read.family for read in fake.log} - GLOBAL)


def endpoints():
    """One endpoint the collector read, per REST family."""
    fake = FakeGitHub()
    collector(fake).collect()
    return {read.family: read.endpoint for read in fake.log if read.family not in GLOBAL | {"graphql"}}


def pull(github):
    rows = [row for repository in github["repositories"] for row in repository.get("pulls", [])]
    return rows[0] if rows else {}


def check_names(row):
    return sorted(check["name"] for check in row.get("checks", []))


class RefusalScope(unittest.TestCase):
    def test_the_collector_asks_for_more_than_its_own_evidence(self):
        kinds = families()
        self.assertGreaterEqual(len(set(kinds) - CORE), 6, kinds)
        self.assertGreaterEqual(len(set(kinds) & CORE), 5, kinds)

    def test_a_spent_family_pauses_only_that_family(self):
        reads = endpoints()
        for kind in families():
            with self.subTest(family=kind):
                fake = FakeGitHub()
                reader = collector(fake)
                reader.collect()
                fake.refuse[kind] = "spent"
                reader.collect()
                reader.api.begin()
                for other, endpoint in reads.items():
                    if other == kind:
                        continue
                    try:
                        reader.api.one(endpoint)
                    except ApiError as error:
                        self.assertNotEqual(error.code, "rate_limited", "a spent %s paused %s" % (kind, other))

    def test_a_refused_extra_read_keeps_the_pull_request_and_its_checks(self):
        kinds = [name for name in families() if name not in LOST_ACCESS]
        for kind in kinds:
            if any(kind.startswith(parent + "/") for parent in kinds):
                continue  # refused with its parent: one permission serves both (a ruleset's history and its versions)
            for refusal in ("forbidden", "plan"):
                with self.subTest(family=kind, refusal=refusal):
                    fake = FakeGitHub()
                    fake.refuse.update({name: refusal for name in kinds if name == kind or name.startswith(kind + "/")})
                    page = service(fake)
                    # Refused from the first pass, so nothing read before can hide it; the second pass shows a
                    # refusal that cleared what was kept.
                    for _ in range(2):
                        row = pull(page.snapshot(force=True)["github"])
                        self.assertEqual(row.get("number"), 3, "a %s on %s dropped the pull request" % (refusal, kind))
                        self.assertEqual(check_names(row), CHECKS)

    def test_a_failed_read_keeps_the_last_sample(self):
        for kind in [*GLOBAL, *families()]:
            with self.subTest(family=kind):
                fake = FakeGitHub()
                page = service(fake)
                before = pull(page.snapshot(force=True)["github"])
                fake.refuse[kind] = "unavailable"
                after = pull(page.snapshot(force=True)["github"])
                self.assertEqual(check_names(before), CHECKS)
                self.assertEqual(after.get("number"), 3, "a 502 on %s dropped the pull request" % kind)
                self.assertEqual(check_names(after), CHECKS)


if __name__ == "__main__":
    unittest.main()
