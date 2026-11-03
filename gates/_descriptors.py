#!/usr/bin/env python3
"""Tool descriptors: a gate that only runs an outside tool, written as data and run by this one runner.

    _descriptors.py GATE [--repo DIR] [--base-ref REF] [--format text|github|json] [--soak] [--all]

A descriptor is a JSON object in gates/descriptors/<gate>.json (the catalog's) or .vibe-verifier-tools/<gate>.json (a
repository's own), with these keys, `options` the only optional one:

    gate         the gate's name in a manifest, which is also the file's name
    description  what the gate fails on
    tool         a tool pinned in gates/_tools.py (a catalog descriptor must name one); a repository's own descriptor
                 may name a command on PATH instead, one its job installs. The tool itself, by a bare name: never a path, and
                 never a launcher such as env or npx, which the duplicate check below cannot see through
    args         the tool's arguments; the judged files follow them
    files        globs of the tracked files it judges (gates/_contract.py glob_to_regex)
    output       how the report on stdout reads: {"parser": "sarif"}, or {"parser": "json", "message": TEMPLATE,
                 "path": KEY, "line": KEY} for a JSON list of objects (null is none), the message a str.format
                 template of each object's keys (a missing key reads "?"); or a named parser for one tool's report,
                 {"parser": "zizmor"}
    options      {"--flag": "help"}: flags the gate takes, each with one value, passed to the tool as written
                 (`--flag value`, before the files) when the manifest line gives them

The runner owns the rest, the same for every descriptor:

- the files: those the pull request added, copied, modified or renamed since the base that match `files`, or every
  tracked one with --all. With none, the gate passes and the tool never runs. A finding the tool reports on any other
  file is dropped, so a tool that reads the whole tree still judges only these; an absolute path it reports is read
  relative to the repository first.
- the exit contract: the tool runs in the repository. Exit 0 or 1 is a report, read whatever the exit (a SARIF tool
  often exits 0 with results); any other exit is "could not run", with its last line of stderr. A report that cannot
  be read, or exit 1 with no finding in it, is "could not run" too, never a pass.
- the parsers (gates/_parsers.py): `sarif` reads SARIF 2.1.0, each result's ruleId, message and first location; a result with a
  suppression is not a finding, and one of level note or none is advisory. `json` is described above. `zizmor` reads
  zizmor's JSON report: each item not ignored, at its primary location, with its severity and confidence.

No duplicates: a descriptor may share neither its gate name nor its `tool` with another descriptor or with a Python gate
(the tools a Python gate runs are those gates/*.py pass to gates/_tools.py's ensure, ensure_node_tool or
ensure_python_tool). Each descriptor gate in such a conflict cannot run until one side goes.

A repository's own descriptors are base-controlled, like its repo-rules files: one the base has is judged as the base
has it, even when the pull request edits or deletes it; one the pull request adds runs at once. With no base to resolve,
the working tree's are read. Every descriptor is read on every run, for the conflict check, so one that cannot be read
stops every descriptor gate in that repository.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys

from _contract import CannotRun, changed_files, git, in_repository, matches, resolve_base, run_gate, tracked_files
from _parsers import PARSERS
from _tools import TOOLS, ensure

GATES = os.path.dirname(os.path.abspath(__file__))
CATALOG = os.path.join(GATES, "descriptors")
REPOSITORY = ".vibe-verifier-tools"
NAME = re.compile(r"^[a-z0-9-]+$")
TOOL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")  # a bare command name: a path would dodge the duplicate check
KEYS = {"gate", "description", "tool", "args", "files", "output"}
OPTIONAL = {"options"}
OUTPUTS = {"sarif": {"parser"}, "json": {"parser", "message", "path", "line"}, "zizmor": {"parser"}}
FLAG = re.compile(r"^--[a-z][a-z0-9-]*$")
RESERVED = {"--repo", "--base-ref", "--format", "--soak", "--all", "--help"}  # the flags every descriptor gate has
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
    if not isinstance(descriptor, dict) or not KEYS <= set(descriptor) <= KEYS | OPTIONAL:
        return "a descriptor has the keys %s, and may have %s" % (", ".join(sorted(KEYS)), ", ".join(sorted(OPTIONAL)))
    stem = os.path.basename(origin)[:-len(".json")]
    if descriptor["gate"] != stem or not NAME.match(stem):
        return "`gate` must be the file's own name, in lowercase letters, digits and dashes"
    if not all(isinstance(descriptor[key], str) and descriptor[key] for key in ("description", "tool")):
        return "`description` and `tool` must be non-empty strings"
    if not TOOL.match(descriptor["tool"]):
        return "`tool` must be a bare command name, not a path"
    if not (_strings(descriptor["args"]) and _strings(descriptor["files"]) and descriptor["files"]):
        return "`args` must be a list of strings and `files` a non-empty one"
    problem = _output_problem(descriptor["output"]) or _options_problem(descriptor.get("options", {}))
    if problem:
        return problem
    if pinned and descriptor["tool"] not in TOOLS:
        return "a catalog descriptor runs a tool pinned in gates/_tools.py, and %r is not" % descriptor["tool"]
    return None


def _output_problem(output):
    output = output if isinstance(output, dict) else {}
    parser = output.get("parser")
    wanted = OUTPUTS.get(parser) if isinstance(parser, str) else None
    if wanted is None or set(output) != wanted or not all(isinstance(value, str) for value in output.values()):
        return ('`output` must be {"parser": "sarif"}, {"parser": "zizmor"} or '
                '{"parser": "json", "message": ..., "path": ..., "line": ...}')
    return None


def _options_problem(options):
    if not isinstance(options, dict) or not all(isinstance(text, str) and text for text in options.values()):
        return "`options` must map each flag to its help text"
    taken = [flag for flag in options if not FLAG.match(flag) or flag in RESERVED]
    if taken:
        return "`options` cannot declare %s: a flag is --lowercase-words, and not one every gate has" % ", ".join(taken)
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


def repository_path(path, repo):
    """A reported path as the repository's own: an absolute one relative to the repository, both resolved, since a
    tool reports the real path of a repository reached through a link (macOS /tmp is /private/tmp)."""
    if os.path.isabs(path):
        path = os.path.relpath(os.path.realpath(path), os.path.realpath(repo))
    return os.path.normpath(path)


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


def _dest(flag):
    return flag[2:].replace("-", "_")


def checker(gate, peeked):
    """The check of one descriptor gate: `peeked` is the descriptor read before the flags were parsed, or None when it
    could not be read, and then it is read again here so the run says why it cannot."""
    def check(args):
        descriptor = peeked or descriptor_for(args.repo, args.base_ref, gate)
        files = judged_files(args, descriptor["files"])
        if not files:
            return []
        passed = [word for flag in descriptor.get("options", {}) if getattr(args, _dest(flag)) is not None
                  for word in (flag, getattr(args, _dest(flag)))]
        result = subprocess.run([resolve(descriptor["tool"]), *descriptor["args"], *passed, *files], cwd=args.repo,
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
        findings = report_findings(descriptor, result, args.repo)
        for finding in findings:
            finding.path = finding.path and repository_path(finding.path, args.repo)
        return [finding for finding in findings if finding.path is None or finding.path in set(files)]
    return check


def arguments(descriptor):
    def add_arguments(parser):
        parser.add_argument("--all", action="store_true", help="judge every tracked file the descriptor's globs match")
        for flag, text in (descriptor or {}).get("options", {}).items():
            parser.add_argument(flag, dest=_dest(flag), metavar="VALUE", default=None, help=text)
    return add_arguments


def peek(gate, argv):
    """The descriptor this run judges with, read before its flags are parsed so that its options can be declared; None
    when it cannot be read yet."""
    early = argparse.ArgumentParser(add_help=False)
    early.add_argument("--repo", default=".")
    early.add_argument("--base-ref", default=None)
    known, _ = early.parse_known_args(argv)
    try:
        return descriptor_for(known.repo, known.base_ref, gate)
    except CannotRun:
        return None


if __name__ == "__main__":
    if len(sys.argv) < 2 or not NAME.match(sys.argv[1]):
        print("usage: _descriptors.py GATE [--repo DIR] [--base-ref REF] [--format text|github|json] [--soak] [--all]",
              file=sys.stderr)
        sys.exit(2)
    name = sys.argv.pop(1)
    found = peek(name, sys.argv[1:])
    run_gate(name, (found or {}).get("description") or "the tool descriptor %s (gates/_descriptors.py)" % name,
             checker(name, found), arguments(found))
