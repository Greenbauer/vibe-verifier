"""The gate contract, shared by every gate in this catalog.

A gate is a command-line program. It reads a working tree and git history and
nothing else: no secrets, no network, no services.

    exit 0   pass
    exit 1   violations found
    exit 2   could not run (misconfigured, missing history, crashed)

--soak reports violations but exits 0, so a new gate can collect signal before
it blocks anything. It never masks exit 2. A gate that cannot run has produced
no signal, and a soak that is silently broken looks exactly like a soak that is
clean: a mutation soak in a sibling project produced no score for five days
before anyone noticed.

--artifacts DIR, on a gate that takes it, is a run's evidence (a QAE explorer's step logs,
screenshots and browser records), and the contract judges none that the checkout supplied:
see planted.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import traceback

EXIT_PASS = 0
EXIT_VIOLATIONS = 1
EXIT_CANNOT_RUN = 2


# The file a runner names for a gate to leave a note in when its pass checked less than it could.
QUALIFIER = "VIBE_VERIFIER_QUALIFIER"


def qualify(note):
    """Mark this run's pass as partial. The runner's summary then reads `PASS (<note>)` instead of a
    bare PASS, so a check that verified less than it could never looks like a full one. Outside a
    runner it does nothing: the gate's own output says it too."""
    path = os.environ.get(QUALIFIER)
    if path:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(note)


class CannotRun(Exception):
    """The gate cannot produce a verdict. Always exit 2, even under --soak."""


class Finding:
    """One finding. An advisory one is printed (as a warning) but never fails the gate."""

    def __init__(self, message, path=None, line=None, advisory=False):
        self.message = message
        self.path = path
        self.line = line
        self.advisory = advisory

    def as_dict(self):
        return {"message": self.message, "path": self.path, "line": self.line, "advisory": self.advisory}


def git(repo, *args):
    try:
        out = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    except FileNotFoundError:
        raise CannotRun("git is not installed")
    if out.returncode != 0:
        raise CannotRun("git %s failed: %s" % (" ".join(args), out.stderr.strip()))
    return out.stdout


def _is_commit(repo, rev):
    probe = subprocess.run(
        ["git", "-C", repo, "rev-parse", "--verify", "--quiet", rev + "^{commit}"],
        capture_output=True,
    )
    return probe.returncode == 0


def resolve_base(repo, explicit=None):
    """The ref a differential gate compares HEAD against.

    An explicit --base-ref must resolve: a named base that does not exist is a
    misconfiguration, and falling back silently would check the wrong diff.
    Otherwise: $VIBE_VERIFIER_BASE_REF, $GITHUB_BASE_REF, main, master, HEAD~1.
    """
    if explicit:
        for candidate in ("origin/" + explicit, explicit):
            if _is_commit(repo, candidate):
                return candidate
        raise CannotRun("--base-ref %r does not resolve to a commit" % explicit)
    names = [os.environ.get("VIBE_VERIFIER_BASE_REF"), os.environ.get("GITHUB_BASE_REF"), "main", "master"]
    for name in filter(None, names):
        for candidate in ("origin/" + name, name):
            if _is_commit(repo, candidate):
                return candidate
    if _is_commit(repo, "HEAD~1"):
        return "HEAD~1"
    raise CannotRun("no base ref to compare against: pass --base-ref, or fetch history (fetch-depth: 0)")


def _paths(repo, *diff_args):
    """Paths from a NUL-separated git diff. Without -z git quotes a non-ASCII path ("src/caf\\303\\251.ts"),
    which then matches nothing in the tracked set and is silently skipped."""
    return [p for p in git(repo, "diff", "-z", *diff_args).split("\0") if p]


def added_files(repo, base):
    return _paths(repo, "--name-only", "--diff-filter=A", base + "...HEAD")


def changed_files(repo, base, *pathspec):
    """Files added, copied, modified or renamed since the base, as tracked paths at HEAD; a pathspec narrows them."""
    return _paths(repo, "--name-only", "--diff-filter=ACMR", base + "...HEAD", "--", *pathspec)


def renamed(repo, base):
    """{path at HEAD: path at the base} for files renamed since the base.

    A ratchet compares a moved file with itself at its old path; looked up by the new path it
    would count as new, and moving an unchanged long or tangled file would block the move. A move
    that also rewrites most of the file is below git's similarity threshold, reports as new, and
    is judged as new: that is a rewrite.
    """
    return _rename_pairs(_paths(repo, "--name-status", "-M", base + "...HEAD"))


def staged_changes(repo):
    """The staged counterpart of changed_files and renamed, for a pre-commit check: (files added,
    copied, modified or renamed in the index against HEAD, {path in the index: path at HEAD})."""
    files = _paths(repo, "--cached", "--name-only", "--diff-filter=ACMR")
    return files, _rename_pairs(_paths(repo, "--cached", "--name-status", "-M"))


def _rename_pairs(fields):
    """{new path: old path} from NUL-separated --name-status fields: status, old, new for a rename."""
    pairs, i = {}, 0
    while i < len(fields):
        if fields[i].startswith("R"):
            pairs[fields[i + 2]] = fields[i + 1]
            i += 3
        else:
            i += 2
    return pairs


def glob_to_regex(glob):
    """A gitignore-flavoured glob: `**/` any directories, `*` within one segment, `?` one character."""
    out, i = "", 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif glob.startswith("**", i):
            out, i = out + ".*", i + 2
        elif glob[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif glob[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(glob[i]), i + 1
    return re.compile("^" + out + "$")


# What a Python test file is called, around the stem of what it tests: pytest's two names, and the
# hyphen and dot forms of test files that are run by path. Every gate that tells a Python test from
# source reads these, so new-source-has-test, cognitive-complexity and changed-code-mutation agree.
PY_TEST_NAMES = ("test_%s.py", "%s_test.py", "test-%s.py", "%s-test.py", "%s.test.py")
PY_TESTS = ["**/" + name % "*" for name in PY_TEST_NAMES]


def matches(path, globs):
    return any(glob_to_regex(glob).match(path) for glob in globs)


def tracked_files(repo):
    return [p for p in git(repo, "ls-files", "-z").split("\0") if p]


def _esc_data(text):
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _esc_prop(text):
    return _esc_data(text).replace(":", "%3A").replace(",", "%2C")


def _annotation(gate, finding, soak):
    props = []
    if finding.path:
        props.append("file=" + _esc_prop(finding.path))
        if finding.line:
            props.append("line=%d" % finding.line)
    props.append("title=" + _esc_prop(gate))
    return "::%s %s::%s" % ("warning" if soak else "error", ",".join(props), _esc_data(finding.message))


def _text(gate, finding):
    where = ""
    if finding.path:
        where = finding.path + (":%d" % finding.line if finding.line else "") + ": "
    return "%s: %s%s%s" % (gate, where, "advisory: " if finding.advisory else "", finding.message)


def report(gate, findings, fmt, soak):
    blocking = [f for f in findings if not f.advisory]
    if fmt == "json":
        status = "pass" if not blocking else ("soak" if soak else "violations")
        print(json.dumps({"gate": gate, "status": status, "findings": [f.as_dict() for f in findings]}, indent=2))
    else:
        _print_lines(gate, findings, blocking, fmt, soak)
    return EXIT_PASS if (soak or not blocking) else EXIT_VIOLATIONS


def _print_lines(gate, findings, blocking, fmt, soak):
    """The text and github formats: a line per finding, then the counts."""
    for finding in findings:
        print(_annotation(gate, finding, soak or finding.advisory) if fmt == "github" else _text(gate, finding))
    if blocking:
        print("%s: %d finding(s), %s" % (gate, len(blocking), "reported only (--soak)" if soak else "blocking"))
    if len(findings) > len(blocking):
        print("%s: %d advisory finding(s), not blocking" % (gate, len(findings) - len(blocking)))


def planted(repo, artifacts):
    """One finding per thing in the evidence directory `artifacts` that the checkout `repo` supplied rather than the
    run: each path the branch tracks at, under or on the way to it (a tracked parent is a link or a submodule, since
    Git lists no plain directory), and each symbolic link in it, the directory itself included.

    The QAE jobs keep the evidence in qae-artifacts/ inside the checkout of the pull request being judged
    (harnesses/qae/). A file the pull request commits there that no explorer writes survives into the evidence,
    and a link it commits (a file, a directory, or the directory itself) points the gate at whatever it names.
    So run_gate judges nothing until this is empty. A run's own evidence is regular files the workflow wrote."""
    found = []
    parent, name = os.path.split(os.path.abspath(artifacts))  # the directory itself unresolved: a link is a finding
    rel = os.path.relpath(os.path.join(os.path.realpath(parent), name), os.path.realpath(repo)).replace(os.sep, "/")
    if rel != ".." and not rel.startswith("../"):
        found += ["the pull request tracks %s" % path for path in sorted(tracked_files(repo))
                  if path == rel or path.startswith(rel + "/") or rel.startswith(path + "/")]
    if os.path.islink(artifacts):
        found.append("%s is a symbolic link" % artifacts)
    else:
        for folder, folders, files in os.walk(artifacts):
            found += ["%s is a symbolic link" % os.path.relpath(os.path.join(folder, name), artifacts)
                      for name in sorted(folders + files) if os.path.islink(os.path.join(folder, name))]
    return [Finding("the evidence in %s is not only the run's own, so none of it is judged: %s. It holds what "
                    "the explorers wrote and nothing else: remove that from the pull request" % (artifacts, why))
            for why in found]


def run_gate(gate, description, check, add_arguments=None):
    """Parse the common flags, run check(args) -> [Finding], exit per the contract.

    argparse already exits 2 on bad usage, which is the contract's "misconfigured".
    """
    parser = argparse.ArgumentParser(prog=gate, description=description)
    parser.add_argument("--repo", default=".", help="repository root (default: current directory)")
    parser.add_argument("--base-ref", default=None, help="ref to diff against (differential gates)")
    parser.add_argument("--format", choices=["text", "github", "json"], default="text")
    parser.add_argument("--soak", action="store_true", help="report violations but exit 0")
    if add_arguments:
        add_arguments(parser)
    args = parser.parse_args()
    try:
        evidence = getattr(args, "artifacts", None)
        findings = (planted(args.repo, evidence) if evidence else []) or check(args)
    except CannotRun as reason:
        message = "%s could not run: %s" % (gate, reason)
    except Exception:
        traceback.print_exc()
        message = "%s crashed (traceback above)" % gate
    else:
        sys.exit(report(gate, findings, args.format, args.soak))
    print("::error title=%s::%s" % (_esc_prop(gate), _esc_data(message)) if args.format == "github" else message,
          file=sys.stdout if args.format == "github" else sys.stderr)
    sys.exit(EXIT_CANNOT_RUN)
