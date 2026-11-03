#!/usr/bin/env python3
"""Tool descriptors: a gate that only runs an outside tool, written as data and run by this one runner.

    _descriptors.py GATE [--repo DIR] [--base-ref REF] [--format text|github|json] [--soak] [--all]

A descriptor is a JSON object in gates/descriptors/<gate>.json (the catalog's) or .vibe-verifier-tools/<gate>.json (a
repository's own), with exactly these keys:

    gate         the gate's name in a manifest, which is also the file's name
    description  what the gate fails on
    tool         a tool pinned in gates/_tools.py (a catalog descriptor must name one); a repository's own descriptor
                 may name a command on PATH instead, one its job installs
    args         the tool's arguments; the judged files follow them
    files        globs of the tracked files it judges (gates/_contract.py glob_to_regex)
    output       how the report on stdout reads: {"parser": "sarif"}, or {"parser": "json", "message": TEMPLATE,
                 "path": KEY, "line": KEY} for a JSON list of objects (null is none), the message a str.format
                 template of each object's keys (a missing key reads "?")

The runner owns the rest, the same for every descriptor:

- the files: those the pull request added, copied, modified or renamed since the base that match `files`, or every
  tracked one with --all. With none, the gate passes and the tool never runs.
- the exit contract: the tool runs in the repository. Exit 0 or 1 is a report, read whatever the exit (a SARIF tool
  often exits 0 with results); any other exit is "could not run", with its last line of stderr. A report that cannot
  be read, or exit 1 with no finding in it, is "could not run" too, never a pass.
- the parsers: `sarif` reads SARIF 2.1.0, each result's ruleId, message and first location; a result with a
  suppression is not a finding, and one of level note or none is advisory. `json` is described above.

No duplicates: a descriptor may share neither its gate name nor its tool with another descriptor or with a Python gate
(the tools a Python gate runs are those gates/*.py pass to gates/_tools.py's ensure, ensure_node_tool or
ensure_python_tool). Each descriptor gate in such a conflict cannot run until one side goes.

A repository's own descriptors are base-controlled, like its repo-rules files: one the base has is judged as the base
has it, even when the pull request edits or deletes it; one the pull request adds runs at once. With no base to resolve,
the working tree's are read. Every descriptor is read on every run, for the conflict check, so one that cannot be read
stops every descriptor gate in that repository.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse

from _contract import CannotRun, Finding, changed_files, git, in_repository, matches, resolve_base, run_gate, tracked_files
from _tools import TOOLS, ensure

GATES = os.path.dirname(os.path.abspath(__file__))
CATALOG = os.path.join(GATES, "descriptors")
REPOSITORY = ".vibe-verifier-tools"
NAME = re.compile(r"^[a-z0-9-]+$")
KEYS = {"gate", "description", "tool", "args", "files", "output"}
OUTPUTS = {"sarif": {"parser"}, "json": {"parser", "message", "path", "line"}}
# A tool a Python gate runs: the literal, or a constant of that file, it passes to one of gates/_tools.py's resolvers.
ENSURED = re.compile(r'\bensure(?:_node_tool|_python_tool)?\(\s*(?:"([^"]+)"|([A-Z_]+)\b)')
CONSTANT = re.compile(r'^([A-Z_]+) = "([^"]+)"$', re.M)
RULE = {"gate": "two gates may not share a name", "tool": "two gates may not run one tool"}


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def python_gates():
    return sorted(name[:-3].replace("_", "-") for name in os.listdir(GATES) if name.endswith(".py") and not name.startswith("_"))


def catalog_names():
    return sorted(name[:-5] for name in os.listdir(CATALOG) if name.endswith(".json"))


def gate_tools():
    """{tool: the gates/ file that runs it} for every tool a Python file in gates/ passes to a resolver."""
    found = {}
    for name in sorted(os.listdir(GATES)):
        if not name.endswith(".py") or name == os.path.basename(__file__):
            continue
        text = read(os.path.join(GATES, name))
        constants = dict(CONSTANT.findall(text))
        for literal, constant in ENSURED.findall(text):
            tool = literal or constants.get(constant)
            if tool:
                found.setdefault(tool, "gates/" + name)
    return found


def _strings(value):
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _problem(descriptor, origin, pinned):
    """What is wrong with a parsed descriptor, or None."""
    if not isinstance(descriptor, dict) or set(descriptor) != KEYS:
        return "a descriptor has exactly the keys %s" % ", ".join(sorted(KEYS))
    stem = os.path.basename(origin)[:-len(".json")]
    if descriptor["gate"] != stem or not NAME.match(stem):
        return "`gate` must be the file's own name, in lowercase letters, digits and dashes"
    if not all(isinstance(descriptor[key], str) and descriptor[key] for key in ("description", "tool")):
        return "`description` and `tool` must be non-empty strings"
    if not (_strings(descriptor["args"]) and _strings(descriptor["files"]) and descriptor["files"]):
        return "`args` must be a list of strings and `files` a non-empty one"
    output = descriptor["output"] if isinstance(descriptor["output"], dict) else {}
    parser = output.get("parser")
    wanted = OUTPUTS.get(parser) if isinstance(parser, str) else None
    if wanted is None or set(output) != wanted or not all(isinstance(value, str) for value in output.values()):
        return '`output` must be {"parser": "sarif"} or {"parser": "json", "message": ..., "path": ..., "line": ...}'
    if pinned and descriptor["tool"] not in TOOLS:
        return "a catalog descriptor runs a tool pinned in gates/_tools.py, and %r is not" % descriptor["tool"]
    return None


def parse(text, origin, pinned):
    try:
        descriptor = json.loads(text)
    except ValueError as error:
        raise CannotRun("%s is not JSON: %s" % (origin, error))
    problem = _problem(descriptor, origin, pinned)
    if problem:
        raise CannotRun("%s: %s" % (origin, problem))
    return descriptor


def catalog():
    """[(origin, descriptor)] for the catalog's descriptors."""
    return [("gates/descriptors/%s.json" % name, parse(read(os.path.join(CATALOG, name + ".json")),
                                                       "gates/descriptors/%s.json" % name, pinned=True))
            for name in catalog_names()]


def _in_repository(path):
    return os.path.dirname(path) == REPOSITORY and path.endswith(".json")


def repository_files(repo, base_ref):
    """{path: (origin, text)} for the repository's own descriptors as this run judges them (base-controlled)."""
    if not in_repository(repo):
        return {}
    try:
        base = resolve_base(repo, base_ref)
    except CannotRun:
        base = None
    head = [path for path in tracked_files(repo) if _in_repository(path)]
    files = {}
    if base:
        for path in git(repo, "ls-tree", "-r", "-z", "--name-only", base, "--", REPOSITORY).split("\0"):
            if _in_repository(path):
                files[path] = ("%s:%s" % (base, path), git(repo, "show", "%s:%s" % (base, path)))
    for path in head:
        text = read(os.path.join(repo, path))
        if path in files and files[path][1] != text:
            print("%s is judged as it is at %s; this branch's change to it applies once it merges" % (path, base),
                  file=sys.stderr)
        files.setdefault(path, (path, text))
    return files


def repository_names(repo, base_ref):
    """The gate names of the repository's own descriptors, unread: a runner lists them before any runs."""
    return sorted(os.path.basename(path)[:-len(".json")] for path in repository_files(repo, base_ref))


def conflicts(descriptors):
    """{descriptor gate: why} for each descriptor that shares its gate name or its tool with another descriptor or a
    Python gate."""
    claims = {}
    for name in python_gates():
        claims.setdefault(("gate", name), []).append(("gates/%s.py" % name.replace("-", "_"), None))
    for tool, path in gate_tools().items():
        claims.setdefault(("tool", tool), []).append((path, None))
    for origin, descriptor in descriptors:
        for key in (("gate", descriptor["gate"]), ("tool", descriptor["tool"])):
            claims.setdefault(key, []).append((origin, descriptor["gate"]))
    why = {}
    for (kind, value), holders in sorted(claims.items()):
        if len(holders) < 2:
            continue
        for _, gate in holders:
            if gate:
                why.setdefault(gate, "%s %r is in %s, and %s: remove the new one, or extend the existing gate"
                               % (kind, value, " and ".join(origin for origin, _ in holders), RULE[kind]))
    return why


def descriptor_for(repo, base_ref, gate):
    files = repository_files(repo, base_ref)
    found = [(origin, parse(text, origin, pinned=False)) for origin, text in (files[path] for path in sorted(files))]
    found += catalog()
    why = conflicts(found)
    if gate in why:
        raise CannotRun(why[gate])
    named = [descriptor for _, descriptor in found if descriptor["gate"] == gate]
    if not named:
        raise CannotRun("no descriptor named %s in gates/descriptors/ or %s/" % (gate, REPOSITORY))
    return named[0]


def resolve(tool):
    if tool in TOOLS:
        return ensure(tool)
    found = shutil.which(tool)
    if not found:
        raise CannotRun("%s is not on PATH; a repository's own descriptor runs a tool its job installs" % tool)
    return found


class _Fields(dict):
    def __missing__(self, key):
        return "?"


def _line(value):
    return value if isinstance(value, int) and value > 0 else None


def json_records(text, output, repo):
    records = json.loads(text) if text.strip() else None
    if records is None:
        return []
    if not isinstance(records, list) or not all(isinstance(record, dict) for record in records):
        raise ValueError("the report is not a list of objects")
    return [Finding(output["message"].format_map(_Fields(record)), record.get(output["path"]) or None,
                    _line(record.get(output["line"]))) for record in records]


def _sarif_path(uri, repo):
    """A SARIF artifact URI as a repository path: percent-decoded, and relative to the repository if it is a file URI."""
    if not uri:
        return None
    parsed = urllib.parse.urlparse(uri)
    path = urllib.parse.unquote(parsed.path)
    return os.path.relpath(path, os.path.abspath(repo)) if parsed.scheme == "file" else path


def _sarif_finding(result, repo):
    location = ((result.get("locations") or [{}])[0] or {}).get("physicalLocation") or {}
    rule, message = result.get("ruleId"), (result.get("message") or {}).get("text") or "?"
    return Finding("%s: %s" % (rule, message) if rule else message,
                   _sarif_path((location.get("artifactLocation") or {}).get("uri"), repo),
                   _line((location.get("region") or {}).get("startLine")), advisory=result.get("level") in ("note", "none"))


def sarif(text, output, repo):
    report = json.loads(text) if text.strip() else {"runs": []}
    return [_sarif_finding(result, repo) for run in report["runs"] for result in run.get("results") or []
            if not result.get("suppressions")]


PARSERS = {"json": json_records, "sarif": sarif}


def report_findings(descriptor, result, repo):
    """The findings of one run of the tool, by the exit contract in this module's docstring."""
    tool, parser = descriptor["tool"], descriptor["output"]["parser"]
    if result.returncode not in (0, 1):
        raise CannotRun("%s exited %d: %s" % (tool, result.returncode, (result.stderr.strip() or "no output").splitlines()[-1]))
    try:
        findings = PARSERS[parser](result.stdout, descriptor["output"], repo)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError) as error:
        raise CannotRun("%s's report could not be read as %s: %s" % (tool, parser, error))
    if result.returncode == 1 and not findings:
        raise CannotRun("%s exited 1 with an empty report" % tool)
    return findings


def judged_files(args, globs):
    names = tracked_files(args.repo) if args.all else changed_files(args.repo, resolve_base(args.repo, args.base_ref))
    return sorted(path for path in names if matches(path, globs))


def checker(gate):
    def check(args):
        descriptor = descriptor_for(args.repo, args.base_ref, gate)
        files = judged_files(args, descriptor["files"])
        if not files:
            return []
        result = subprocess.run([resolve(descriptor["tool"]), *descriptor["args"], *files], cwd=args.repo,
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
        return report_findings(descriptor, result, args.repo)
    return check


def add_arguments(parser):
    parser.add_argument("--all", action="store_true", help="judge every tracked file the descriptor's globs match")


def description(gate):
    path = os.path.join(CATALOG, gate + ".json")
    if os.path.isfile(path):
        try:
            return json.loads(read(path)).get("description")
        except (ValueError, AttributeError):
            return None
    return "a repository's own tool descriptor, %s/%s.json" % (REPOSITORY, gate)


if __name__ == "__main__":
    if len(sys.argv) < 2 or not NAME.match(sys.argv[1]):
        print("usage: _descriptors.py GATE [--repo DIR] [--base-ref REF] [--format text|github|json] [--soak] [--all]",
              file=sys.stderr)
        sys.exit(2)
    name = sys.argv.pop(1)
    run_gate(name, description(name), checker(name), add_arguments)
