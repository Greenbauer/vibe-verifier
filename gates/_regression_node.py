"""node:test runs for regression-test-fails-on-base: which changed files the repository runs with
`node --test` and how, and what each file's tests did, read from Node's TAP reporter.

How a file is run comes from the repository, never from its name alone: a package.json script, in
the file's package or a directory above it, whose `node ... --test ...` command selects it (by path,
glob, directory or, with no paths, Node's default test names), run again from that package with the
command's own options; else a --node-test glob, run as `node --test <file>` from the repository root.
coverage-not-lost reads which files run, and how, from here too (gates/_coverage_node.py).

Each file runs alone (TAP names a passing test but not its file), with
`--test-reporter=tap --test-reporter-destination=<file>`, under Node 20, 22 and 24 alike. TAP is the
built-in reporter that keeps what the verdict needs: each test's outcome, a failure's `failureType`,
its error's `name` and message unwrapped from ERR_TEST_FAILURE, and the stderr of the file's own
process. JUnit drops that stderr and names no error type. A file whose process failed before any test
of its own reported is one entry named for the file, with `exitCode` in its details and the reason
only in that stderr: the file did not load.
"""
import functools
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from collections import defaultdict, namedtuple

from _contract import CannotRun, matches
from _local_guard import Guard
from _regression_runners import ADVICE, Run, first_failure, first_line, remaining
from _tools import run_tool

Plan = namedtuple("Plan", "cwd flags env source")
EXTENSIONS = ("cjs", "mjs", "js", "cts", "mts", "ts")
# `node --test` with no paths, as Node 22 and 24 document it (the TypeScript names unless type stripping is off).
DEFAULT = ["**/%s.%s" % (name, ext) for ext in EXTENSIONS for name in ("*.test", "*-test", "*_test", "test-*", "test")] + \
          ["**/test/**/*." + ext for ext in EXTENSIONS]
# Options whose output or file selection the gate sets itself: the reporter, sharding, watching, coverage, isolation.
DROPPED = ("--test", "--test-shard", "--test-isolation", "--experimental-test-isolation", "--experimental-test-coverage")
DROPPED_PREFIXES = ("--test-reporter", "--watch", "--test-coverage-")
ASSIGNMENT = re.compile(r"^[A-Za-z_]\w*=")
SEPARATOR = re.compile(r"^[();<>|&]+$")
ERROR_LINE = re.compile(r"^(?:[A-Z][\w$]*)?Error(?: \[\w+\])?: ")
RESULT = re.compile(r"^((?:    )*)(ok|not ok) \d+(?: - (.*?))?(?: # (SKIP|TODO)\b.*)?$")
KEY = re.compile(r"^([\w$]+):(?: (.*))?$")


@functools.lru_cache(maxsize=None)
def value_options():
    """The options the Node on PATH reads a separate value for: `node --help` prints each as `--name=...`."""
    try:
        text = subprocess.run(["node", "--help"], capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CannotRun("a package.json script runs `node --test`, and `node --help` could not be read: %s" % error)
    names = set()
    for line in text.splitlines():
        found = re.match(r"^  (-\S.*?)=\.\.\.(?:\s|$)", line)
        if found:
            names.update(name.strip() for name in found.group(1).split(","))
    return frozenset(names)


def commands(script):
    """Each simple command of a package.json script, as its words; none when the script cannot be read."""
    lexer = shlex.shlex(script, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        words = list(lexer)
    except ValueError:
        return []
    found, command = [], []
    for word in words + [";"]:
        if SEPARATOR.match(word):
            found += [command] if command else []
            command = []
        else:
            command.append(word)
    return found


def node_test(words):
    """(env, options, paths) of a `node ... --test ...` command, else None. Node reads its options up to the
    first path; with --test every word after that is a path or glob."""
    env = {}
    while words and ASSIGNMENT.match(words[0]):
        name, value = words[0].split("=", 1)
        env[name], words = value, words[1:]
    if not words or os.path.basename(words[0]) != "node" or any("$" in word or "`" in word for word in words):
        return None
    options, index, testing = [], 1, False
    while index < len(words) and words[index].startswith("-") and words[index] not in ("-", "--"):
        name = words[index].split("=", 1)[0]
        takes = "=" not in words[index] and name in value_options() and index + 1 < len(words)
        option = words[index:index + 1 + takes]
        index += len(option)
        testing = testing or name == "--test"
        if name not in DROPPED and not name.startswith(DROPPED_PREFIXES):
            options += option
    paths = words[index + (index < len(words) and words[index] == "--"):]
    return (env, options, paths) if testing else None


def braces(pattern):
    """A glob's `{a,b}` alternatives expanded, as Node's glob reads them."""
    found = re.search(r"\{([^{}]*)\}", pattern)
    if not found:
        return [pattern]
    return [expanded for choice in found.group(1).split(",")
            for expanded in braces(pattern[:found.start()] + choice + pattern[found.end():])]


def selected_by(pattern, path, package):
    """Whether one path or glob of a `node --test` command (braces expanded) selects `path`."""
    if re.search(r"[*?\[]", pattern):
        return matches(path, [pattern])
    if os.path.isdir(os.path.join(package, pattern)):  # Node 20 searches a directory; 22 and 24 refuse one
        return matches(path, [os.path.normpath(pattern) + "/" + name for name in DEFAULT])
    return os.path.normpath(pattern) == path


def selects(paths, path, package):
    """Whether a `node --test` command run from `package` (absolute) with these paths runs `path` (relative to it)."""
    if not paths:
        return matches(path, DEFAULT)
    return any(selected_by(pattern, path, package)
               for given in paths for pattern in braces(given[2:] if given.startswith("./") else given))


@functools.lru_cache(maxsize=None)
def scripts(package_json):
    """[(name, command)] of a package.json's scripts that mention `node` and `--test`, or [] when it has none."""
    try:
        with open(package_json, encoding="utf-8") as handle:
            found = json.load(handle).get("scripts") or {}
    except (OSError, ValueError, AttributeError):
        return []
    return [(name, text) for name, text in sorted(found.items()) if isinstance(text, str) and "node" in text and "--test" in text]


@functools.lru_cache(maxsize=None)
def plan(repo, path, declared=()):
    """How the repository runs `path` with `node --test`: the Plan, or None when nothing it holds does."""
    root = os.path.realpath(repo)
    directory = os.path.dirname(path)
    while True:
        package = os.path.join(root, directory)
        for name, script in scripts(os.path.join(package, "package.json")):
            for words in commands(script):
                found = node_test(words)
                if found and selects(found[2], os.path.relpath(os.path.join(root, path), package), package):
                    return Plan(package, found[1], found[0], "%s script `%s`" % (os.path.join(directory, "package.json"), name))
        if not directory:
            break
        directory = os.path.dirname(directory)
    return Plan(root, [], {}, "--node-test") if matches(path, declared) else None


def unescape(text):
    return re.sub(r"\\(.)", r"\1", text)


def value(text, block):
    """A TAP detail's value: Node writes a string as a quoted literal on the line, or as a `|-` block."""
    if text == "|-":
        return "\n".join(block)
    if len(text) > 1 and text[0] in "'\"`" and text[-1] == text[0]:
        return re.sub(r"\\(.)", lambda found: {"n": "\n", "t": "\t"}.get(found.group(1), found.group(1)), text[1:-1])
    return "" if text == "~" else text


def details(lines, start, indent):
    """({key: value} of the YAML block at lines[start], the index after it) for a result at `indent`."""
    if start >= len(lines) or lines[start] != indent + "  ---":
        return {}, start
    found, index, key = {}, start + 1, None
    while index < len(lines) and lines[index] != indent + "  ...":
        line = lines[index]
        own = KEY.match(line[len(indent) + 2:]) if line.startswith(indent + "  ") and line[len(indent) + 2:len(indent) + 3] != " " else None
        if own:
            key = own.group(1)
            found[key] = [own.group(2) or ""]
        elif key and line.startswith(indent + "    "):
            found[key].append(line[len(indent) + 4:])
        index += 1
    return {name: value(parts[0], parts[1:]) for name, parts in found.items()}, index + 1


def entries(text):
    """The TAP report's results as a tree ([roots], each {name, failed, skipped, details, children}), and the
    report's top-level comments, each root holding how many came before it (the file's own stderr is among them)."""
    lines, pending, comments, index = text.splitlines(), defaultdict(list), [], 0
    while index < len(lines):
        line, index = lines[index], index + 1
        if line.startswith("# ") and not line.startswith("# Subtest: "):
            comments.append(unescape(line[2:]))
            continue
        found = RESULT.match(line)
        if not found:
            continue
        nesting = len(found.group(1)) // 4
        entry = {"name": unescape(found.group(3) or ""), "failed": found.group(2) == "not ok", "skipped": bool(found.group(4)),
                 "children": pending.pop(nesting + 1, []), "comments": len(comments)}
        entry["details"], index = details(lines, index, found.group(1))
        pending[nesting].append(entry)
    return [entry for nesting in sorted(pending) for entry in pending[nesting]], comments


def described(info):
    """`Type: message` of a failed result: its error's name (Node omits a plain Error's) and first line."""
    lines = [line.strip() for line in str(info.get("error", "")).splitlines() if line.strip()]
    message = " ".join(lines[:2]) if len(lines) > 1 and lines[0].endswith(":") else (lines[0] if lines else "")
    return first_line("%s: %s" % (info.get("name") or "Error", message))


def own_failure(entry):
    """The error of a result that failed on its own account, not because a test under it failed or its parent did."""
    if entry["failed"] and entry["details"].get("failureType") not in ("subtestsFailed", "cancelledByParent"):
        return described(entry["details"])
    return None


def collect(entry, names, inherited, tests):
    """Each test under `entry` into tests: {id: (status, error)}, a test cancelled by its parent taking that
    parent's error (a `before` hook that failed, say)."""
    ids, own = names + [entry["name"]], own_failure(entry)
    if entry["children"]:
        if own:
            tests[" > ".join(ids)] = ("failed", own)
        for child in entry["children"]:
            collect(child, ids, own or inherited, tests)
        return
    status = "skipped" if entry["skipped"] else "failed" if entry["failed"] else "passed"
    cancelled = entry["details"].get("failureType") == "cancelledByParent"
    tests[" > ".join(ids)] = (status, (inherited if cancelled and inherited else described(entry["details"])) if status == "failed" else "")


def tap_run(text, names):
    """The Run of one file's TAP report; `names` are the ones Node may give the file's own entry (the path it
    was given, or its absolute path under Node 20)."""
    roots, comments = entries(text)
    own = [entry for entry in roots if entry["name"] in names and not entry["children"]]
    tests = {}
    for entry in roots:
        if entry not in own:
            collect(entry, [], None, tests)
    failed = [entry for entry in own if entry["failed"]]
    if not failed:
        return Run(None, tests)
    said = [line for line in comments[:failed[0]["comments"]] if ERROR_LINE.match(line)]
    reason = first_line(said[0]) if said else "the file's process exited with code %s before any test reported" % (
        failed[0]["details"].get("exitCode") or "?")
    if tests:  # tests reported, then the process failed: the file is red
        tests["(the test file's own process)"] = ("failed", reason)
        return Run(None, tests)
    return Run(reason, {})


def env_files(how):
    """The files a Plan's `--env-file` and `--env-file-if-exists` options load, as paths."""
    found = []
    for index, flag in enumerate(how.flags):
        name, given, value = flag.partition("=")
        if name in ("--env-file", "--env-file-if-exists"):
            found.append(os.path.join(how.cwd, value if given else (how.flags[index + 1:] or [""])[0]))
    return found


def run_node(repo, plans, deadline, timeout, local_names):
    """Each file in `plans` ({path: Plan}) under `node --test`, alone and guarded, the way its Plan says (its script's
    `NAME=value` assignments and the files its `--env-file` options load are in what the guard checks)."""
    if not shutil.which("node"):
        raise CannotRun("no node on PATH for %s: this gate runs node:test files with the project's own Node" % sorted(plans)[0])
    runs = {}
    for path, how in sorted(plans.items()):
        full = os.path.join(os.path.realpath(repo), path)
        given = os.path.relpath(full, how.cwd)
        scratch = tempfile.mkdtemp(prefix="vv-regression-")
        try:
            out = os.path.join(scratch, "report.tap")
            command = ["node", *how.flags, "--test", "--test-reporter=tap", "--test-reporter-destination=" + out, given]
            with Guard("regression-test-fails-on-base", repo, local_names) as guard:
                code, output = run_tool(command, how.cwd, remaining(deadline, timeout), "node --test",
                                        env=guard.env("node --test of " + path, extra=how.env, files=env_files(how)),
                                        budget=timeout, advice=ADVICE)
                try:
                    with open(out, encoding="utf-8", errors="replace") as handle:
                        text = handle.read()
                except OSError:
                    text = ""
                if not text.startswith("TAP version"):
                    raise CannotRun("node --test wrote no TAP report for %s (run as %s, exit %d): %s"
                                    % (path, how.source, code, output.strip()[-2000:]))
                runs[path] = tap_run(text, {given, full})
                guard.judge(first_failure({path: runs[path]}), "the run of node --test of " + path)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    return runs
