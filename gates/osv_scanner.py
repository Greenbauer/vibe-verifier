#!/usr/bin/env python3
"""Fail when a dependency file this pull request changed pins a known-vulnerable version it did not pin at the base.

osv-scanner, pinned as a release binary (the digests in _tools.TOOLS), reads each changed dependency
file at HEAD and at the merge base against the OSV database (osv.dev): Python requirements files and
poetry.lock, uv.lock, Pipfile.lock, pdm.lock; npm, pnpm and yarn lockfiles. A ratchet per
vulnerability: a finding is a vulnerability of a package version in the file at HEAD that the file did
not have at the base (one per alias group, so a PYSEC id and its GHSA twin are one finding). Adding or
bumping a dependency to a vulnerable version fails; a vulnerability published later against a pin
nobody touched never blocks unrelated work, and --all reports every one, for an audit.

Only what the file states is read (--no-resolve): a requirements line that is not pinned with == is
read at its lower bound, and nothing is resolved through a package index, so the verdict does not
change with what the index serves today. The lookup needs the network; without it, or with a file the
scanner cannot read, the gate cannot run (exit 2). A vulnerability the repository has judged does not
apply is listed with its reason in an osv-scanner.toml beside the file, the scanner's own ignore file,
where the review reads it.

    --source GLOB    what counts as a dependency file (repeatable; default: the names above)
    --exclude GLOB   extra paths to ignore (repeatable; node_modules always is)
"""
import json
import os
import re
import subprocess
import tempfile

from _contract import CannotRun, Finding, changed_files, git, matches, renamed, resolve_base, run_gate, tracked_files
from _tools import ensure

GATE = "osv-scanner"
REQUIREMENTS = ["**/requirements*.txt", "**/requirements/*.txt"]  # read as requirements files whatever the name
DEFAULT_SOURCE = REQUIREMENTS + ["**/" + name for name in ("poetry.lock", "uv.lock", "Pipfile.lock", "pdm.lock",
                                                           "package-lock.json", "pnpm-lock.yaml", "yarn.lock")]
ALWAYS_EXCLUDED = ["**/node_modules/**"]


def scan(binary, root, files):
    """{path: {(package, version, vulnerability): message}} for every known vulnerability in the files under root."""
    if not files:
        return {}
    args = []
    for path in files:
        args += ["--lockfile", ("requirements.txt:" if matches(path, REQUIREMENTS) else "") + path]
    result = subprocess.run([binary, "scan", "source", "--no-resolve", "--format", "json", *args], cwd=root,
                            capture_output=True, text=True)
    if result.returncode not in (0, 1):  # 1: vulnerabilities found
        raise CannotRun("osv-scanner exited %d: %s" % (result.returncode, (result.stderr.strip() or "no output").splitlines()[-1]))
    try:
        report = json.loads(result.stdout)
    except ValueError as error:
        raise CannotRun("osv-scanner's JSON report could not be read: %s" % error)
    real_root = os.path.realpath(root)
    found = {}
    for entry in report.get("results") or []:
        path = os.path.relpath(os.path.realpath(entry["source"]["path"]), real_root)
        for item in entry.get("packages") or []:
            package = item["package"]
            for group in item.get("groups") or []:
                ids = sorted(group["ids"])
                key = (package["name"].lower(), package.get("version", ""), ids[0])
                found.setdefault(path, {})[key] = "%s %s (%s): known vulnerability %s" % (
                    package["name"], package.get("version", "?"), package.get("ecosystem", "?"), " / ".join(ids))
    return found


def line_of(repo, path, package):
    """The first line of the file naming the package, for the annotation; None when it cannot be told."""
    name = re.compile(r"(?<![\w.-])%s(?![\w-])" % re.sub(r"[-_.]", "[-_.]", re.escape(package)), re.IGNORECASE)
    with open(os.path.join(repo, path), encoding="utf-8", errors="replace") as handle:
        return next((number for number, text in enumerate(handle, 1) if name.search(text)), None)


def scan_at_base(binary, repo, base, files):
    """scan() of the files as they were at the merge base, each renamed file at its old path; a file new in this
    pull request is absent, so every vulnerability in it counts."""
    moved = renamed(repo, base)
    start = git(repo, "merge-base", base, "HEAD").strip()
    with tempfile.TemporaryDirectory(prefix="vv-base-") as snapshot:
        at_base = []
        for path in files:
            origin = moved.get(path, path)
            if subprocess.run(["git", "-C", repo, "cat-file", "-e", "%s:%s" % (start, origin)], capture_output=True).returncode:
                continue
            target = os.path.join(snapshot, path)  # same name: the scanner tells a lockfile's format by it
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(git(repo, "show", "%s:%s" % (start, origin)))
            at_base.append(path)
        return scan(binary, snapshot, at_base)


def findings(repo, found, before):
    return [Finding(message, path, line_of(repo, path, key[0]))
            for path, vulnerabilities in sorted(found.items())
            for key, message in sorted(vulnerabilities.items()) if key not in before.get(path, {})]


def check(args):
    tracked = set(tracked_files(args.repo))
    excluded = ALWAYS_EXCLUDED + (args.exclude or [])
    source = args.source or DEFAULT_SOURCE
    if args.all:
        files = sorted(p for p in tracked if matches(p, source) and not matches(p, excluded))
        return findings(args.repo, scan(ensure(GATE), args.repo, files), {})
    base = resolve_base(args.repo, args.base_ref)
    files = sorted(p for p in changed_files(args.repo, base) if p in tracked and matches(p, source) and not matches(p, excluded))
    if not files:
        return []
    binary = ensure(GATE)
    return findings(args.repo, scan(binary, args.repo, files), scan_at_base(binary, args.repo, base, files))


def add_arguments(parser):
    parser.add_argument("--source", action="append", metavar="GLOB")
    parser.add_argument("--exclude", action="append", metavar="GLOB")
    parser.add_argument("--all", action="store_true", help="report every known vulnerability in every tracked dependency file")


if __name__ == "__main__":
    run_gate(GATE, __doc__, check, add_arguments)
