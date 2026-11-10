#!/usr/bin/env python3
"""Tool descriptors: a gate that only runs an outside tool, written as data and run by this one runner.

    _descriptors.py GATE [--repo DIR] [--base-ref REF] [--format F] [--soak] [--all] [--source GLOB] [--exclude GLOB]

A descriptor is a JSON object in gates/descriptors/<gate>.json (the catalog's) or .vibe-verifier-tools/<gate>.json (a
repository's own). docs/GATE-CONTRACT.md "Tool descriptors" is the full format; in short:

    gate, description   its manifest name (the file's own name) and what it fails on
    tool                a bare name: a binary pinned in gates/_tools.py, a Python toolchain pinned in tools/<tool>/ (run
                        as `python -m <tool>`), or, for a repository's own descriptor only, a command on PATH
    args, files         the tool's arguments, and globs of the tracked files it judges
    exclude             optional: globs it never judges
    output              {"parser": NAME} (gates/_parsers.py), or {"parser": "json", "message", "path", "line", "rule"?}
    options             optional: {"--flag": help or {"help", "to"?, "default"?, "choices"?}}, each passed to the tool
                        as `<to or flag> value`, before `args`, when it has a value
    ratchet             optional: "count", gates/_ratchet.py

The runner owns the rest: the files (changed since the base, or every tracked one with --all; --source replaces
`files`, --exclude adds to `exclude`; none means a pass without running the tool, and a finding on any other file is
dropped), and the exit contract (exit 0 or 1 is a report, read whatever the exit; any other exit, a report that cannot
be read, or exit 1 with no finding is "could not run", never a pass).

No duplicates: a descriptor may share neither its gate name nor its `tool` with another descriptor or with a Python gate
(the tools a Python gate runs are those gates/*.py pass to gates/_tools.py's ensure, ensure_node_tool or
ensure_python_tool). Each descriptor gate in such a conflict cannot run until one side goes.

A repository's own descriptors are base-controlled, like its repo-rules files: one the base has is judged as the base
has it, even when the pull request edits or deletes it; one the pull request adds runs at once. Every descriptor is read
on every run, for the conflict check, so one that cannot be read stops every descriptor gate in that repository.
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
from _ratchet import count_ratchet
from _tools import NODE_TOOLS, TOOLS, ensure, ensure_python_tool

GATES = os.path.dirname(os.path.abspath(__file__))
CATALOG = os.path.join(GATES, "descriptors")
REPOSITORY = ".vibe-verifier-tools"
NAME = re.compile(r"^[a-z0-9-]+$")
TOOL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")  # a bare command name: a path would dodge the duplicate check
KEYS = {"gate", "description", "tool", "args", "files", "output"}
OPTIONAL = {"options", "exclude", "ratchet"}
OUTPUTS = {"sarif": {"parser"}, "json": {"parser", "message", "path", "line"}, "zizmor": {"parser"}, "bandit": {"parser"},
           "eslint": {"parser"}}
OUTPUT_OPTIONAL = {"json": {"rule"}}  # the record key that names each finding's rule, for the count ratchet
RATCHETS = ("count",)
FLAG = re.compile(r"^--[a-z][a-z0-9-]*$")
OPTION = {"help", "to", "default", "choices"}  # an option given as an object: only `help` is required
# The flags every descriptor gate takes (the runner's own) and the ones gates/_contract.py run_gate reads.
RESERVED = {"--repo", "--base-ref", "--format", "--soak", "--all", "--source", "--exclude", "--help", "--artifacts"}
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
    if not (_strings(descriptor["args"]) and _strings(descriptor["files"]) and descriptor["files"]
            and _strings(descriptor.get("exclude", []))):
        return "`args` and `exclude` must be lists of strings and `files` a non-empty one"
    if descriptor.get("ratchet", "count") not in RATCHETS:
        return "`ratchet` is %s" % " or ".join('"%s"' % kind for kind in RATCHETS)
    problem = _output_problem(descriptor["output"]) or _options_problem(descriptor.get("options", {}))
    if problem:
        return problem
    if pinned and not pinned_tool(descriptor["tool"]):
        return "a catalog descriptor runs a tool pinned in gates/_tools.py or tools/<name>/, and %r is not" % descriptor["tool"]
    return None


def pinned_tool(tool):
    """Whether the catalog pins `tool`: a binary in gates/_tools.py, or a Python toolchain tools/<tool>/requirements.txt."""
    return tool in TOOLS or os.path.isfile(os.path.join(NODE_TOOLS, tool, "requirements.txt"))


def _output_problem(output):
    output = output if isinstance(output, dict) else {}
    parser = output.get("parser")
    wanted = OUTPUTS.get(parser) if isinstance(parser, str) else None
    if wanted is None or not wanted <= set(output) <= wanted | OUTPUT_OPTIONAL.get(parser, set()) \
            or not all(isinstance(value, str) for value in output.values()):
        return ('`output` must be {"parser": NAME} (%s) or {"parser": "json", "message": ..., "path": ..., "line": ...}, '
                'optionally with "rule"' % ", ".join(sorted(name for name in OUTPUTS if name != "json")))
    return None


def option(spec):
    """An option's spec as {help, to, default, choices}: a string is its help text, passed on as the same flag."""
    return {"help": spec} if isinstance(spec, str) else spec


def _option_problem(spec):
    spec = option(spec)
    if not isinstance(spec, dict) or not {"help"} <= set(spec) <= OPTION or not isinstance(spec["help"], str):
        return "an option is its help text, or {\"help\", and optionally \"to\", \"default\", \"choices\"}"
    if not FLAG.match(spec.get("to", "--x")) or not isinstance(spec.get("default", ""), str):
        return "an option's `to` is a --flag and its `default` a string"
    if not _strings(spec.get("choices", [])) or ("default" in spec and spec.get("choices") and spec["default"] not in spec["choices"]):
        return "an option's `choices` are strings, and hold its `default`"
    return None


def _options_problem(options):
    if not isinstance(options, dict):
        return "`options` must map each flag to its help text or spec"
    taken = [flag for flag in options if not FLAG.match(flag) or flag in RESERVED]
    if taken:
        return ("`options` cannot declare %s: a flag is --lowercase-words, and not one the runner or the gate contract "
                "reads" % ", ".join(taken))
    return next(filter(None, (_option_problem(spec) for spec in options.values())), None)


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
    """The command that runs `tool`: a pinned binary, a pinned Python toolchain's `python -m tool`, or one on PATH."""
    if tool in TOOLS:
        return [ensure(tool)]
    if pinned_tool(tool):
        return [ensure_python_tool(tool), "-m", tool]
    found = shutil.which(tool)
    if not found:
        raise CannotRun("%s is not on PATH; a repository's own descriptor runs a tool its job installs" % tool)
    return [found]


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


def judged_files(args, descriptor, base):
    """The files the tool is given: changed since the base (every tracked one with --all) that match `files` (or the
    run's --source globs) and none of `exclude` or the run's --exclude globs."""
    names = tracked_files(args.repo) if args.all else changed_files(args.repo, base)
    included, excluded = args.source or descriptor["files"], descriptor.get("exclude", []) + (args.exclude or [])
    return sorted(path for path in names if matches(path, included) and not matches(path, excluded))


def _dest(flag):
    return flag[2:].replace("-", "_")


def run_tool(descriptor, args, root, files):
    """The findings of the tool on `files` under `root`, each at its path relative to `root`, given only for those files.
    Options come first, then `args`, then the files."""
    passed = []
    for flag, spec in descriptor.get("options", {}).items():
        if getattr(args, _dest(flag)) is not None:
            passed += [option(spec).get("to", flag), getattr(args, _dest(flag))]
    result = subprocess.run([*resolve(descriptor["tool"]), *passed, *descriptor["args"], *files], cwd=root,
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    findings = report_findings(descriptor, result, root)
    for finding in findings:
        finding.path = finding.path and repository_path(finding.path, root)
    return [finding for finding in findings if finding.path is None or finding.path in set(files)]


def checker(gate, peeked):
    """The check of one descriptor gate: `peeked` is the descriptor read before the flags were parsed, or None when it
    could not be read, and then it is read again here so the run says why it cannot."""
    def check(args):
        descriptor = peeked or descriptor_for(args.repo, args.base_ref, gate)
        base = None if args.all else resolve_base(args.repo, args.base_ref)
        files = judged_files(args, descriptor, base)
        if not files:
            return []
        findings = run_tool(descriptor, args, args.repo, files)
        if base is None or "ratchet" not in descriptor:
            return findings
        return count_ratchet(args.repo, base, files, findings, lambda root, paths: run_tool(descriptor, args, root, paths))
    return check


def arguments(descriptor):
    def add_arguments(parser):
        parser.add_argument("--all", action="store_true", help="judge every tracked file the descriptor's globs match")
        parser.add_argument("--source", action="append", metavar="GLOB", help="judge these files instead of the descriptor's")
        parser.add_argument("--exclude", action="append", metavar="GLOB", help="never judge these files (repeatable)")
        for flag, spec in (descriptor or {}).get("options", {}).items():
            spec = option(spec)
            parser.add_argument(flag, dest=_dest(flag), metavar="VALUE", default=spec.get("default"),
                                choices=spec.get("choices"), help=spec["help"])
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
