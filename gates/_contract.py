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


class CannotRun(Exception):
    """The gate cannot produce a verdict. Always exit 2, even under --soak."""


class Finding:
    def __init__(self, message, path=None, line=None):
        self.message = message
        self.path = path
        self.line = line

    def as_dict(self):
        return {"message": self.message, "path": self.path, "line": self.line}


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


def changed_files(repo, base):
    """Files added, copied, modified or renamed since the base, as tracked paths at HEAD."""
    return _paths(repo, "--name-only", "--diff-filter=ACMR", base + "...HEAD")


def renamed(repo, base):
    """{path at HEAD: path at the base} for files renamed since the base.

    A ratchet compares a moved file with itself at its old path; looked up by the new path it
    would count as new, and moving an unchanged long or tangled file would block the move. A move
    that also rewrites most of the file is below git's similarity threshold, reports as new, and
    is judged as new: that is a rewrite.
    """
    fields = _paths(repo, "--name-status", "-M", base + "...HEAD")  # status, old, new for a rename
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
    return "%s: %s%s" % (gate, where, finding.message)


def report(gate, findings, fmt, soak):
    if fmt == "json":
        status = "pass" if not findings else ("soak" if soak else "violations")
        print(json.dumps({"gate": gate, "status": status, "findings": [f.as_dict() for f in findings]}, indent=2))
    else:
        for finding in findings:
            print(_annotation(gate, finding, soak) if fmt == "github" else _text(gate, finding))
        if findings:
            print("%s: %d finding(s), %s" % (gate, len(findings), "reported only (--soak)" if soak else "blocking"))
    return EXIT_PASS if (soak or not findings) else EXIT_VIOLATIONS


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
        findings = check(args)
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
