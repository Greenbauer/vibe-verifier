#!/usr/bin/env python3
"""Fail when the commits since the base ref contain a secret, by gitleaks' rules.

A wired gate: it depends on the gitleaks binary, pinned in gates/_tools.py, and scans only what
this pull request adds (`base..HEAD`), so a secret that already sits in history is a separate
clean-up and never a reason to let a new one in. Rules come from gitleaks' precedence: --config,
then the repository's own .gitleaks.toml, then gitleaks' defaults; a false positive is silenced
where gitleaks documents it (a `gitleaks:allow` comment on the line, or the repository's config).
Secrets are redacted in every report this gate prints.
"""
import json
import subprocess

from _contract import CannotRun, Finding, git, resolve_base, run_gate
from _tools import ensure

GATE = "gitleaks"


def check(args):
    base = resolve_base(args.repo, args.base_ref)
    if git(args.repo, "rev-list", "--count", "%s..HEAD" % base).strip() == "0":
        return []
    binary = ensure("gitleaks")
    command = [binary, "git", args.repo, "--log-opts=%s..HEAD" % base, "--report-format", "json", "--report-path", "-",
               "--exit-code", "1", "--no-banner", "--redact"]
    if args.config:
        command += ["--config", args.config]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode not in (0, 1):
        raise CannotRun("gitleaks exited %d: %s" % (result.returncode, (result.stderr.strip() or "no output").splitlines()[-1]))
    if result.returncode == 0:
        return []
    try:
        leaks = json.loads(result.stdout or "[]")
    except ValueError as error:
        raise CannotRun("gitleaks reported leaks but its JSON report could not be read: %s" % error)
    if not leaks:
        raise CannotRun("gitleaks exited 1 with an empty report")
    return [Finding("%s: %s (commit %s)" % (leak.get("RuleID", "?"), leak.get("Description", "").rstrip("."), str(leak.get("Commit", ""))[:10]),
                    leak.get("File") or None, leak.get("StartLine") or None) for leak in leaks]


def add_arguments(parser):
    parser.add_argument("--config", metavar="FILE", default=None,
                        help="gitleaks config (default: the repository's .gitleaks.toml, else gitleaks' rules)")


if __name__ == "__main__":
    run_gate(GATE, __doc__, check, add_arguments)
