"""`run`, `list` and `tool`, and what every other subcommand reads a manifest with: a manifest's entries, judged from
the base on a pull request (the usage text in bin/vibe-verifier says how)."""
import os
import re
import shlex
import subprocess
import sys
import tempfile

from _contract import QUALIFIER, CannotRun, resolve_base
from _tools import NODE_TOOLS, ensure_node_tool
import repo_rules

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GATES = os.path.join(ROOT, "gates")
LABEL = {0: "PASS", 1: "FAIL", 2: "COULD NOT RUN"}


def available():
    return sorted(name[:-3].replace("_", "-") for name in os.listdir(GATES)
                  if name.endswith(".py") and not name.startswith("_"))


def parse_manifest(path):
    if not os.path.isfile(path):
        sys.exit(fail("no manifest at %s" % path))
    with open(path, encoding="utf-8") as handle:
        entries = parse_entries(handle.read(), path)
    if not entries:
        sys.exit(fail("%s subscribes to no gates" % path))
    return entries


def parse_entries(text, origin):
    """[(gate, [args])] from manifest text; a line shlex cannot read is exit 2."""
    entries = []
    for number, raw in enumerate(text.splitlines(), 1):
        try:
            words = shlex.split(raw, comments=True)
        except ValueError as error:
            sys.exit(fail("%s line %d: %s" % (origin, number, error)))
        if words:
            entries.append((words[0], words[1:]))
    return entries


def base_manifest(args):
    """(base ref, [(gate, [args])]) for the manifest at the base, or (None, None) when there is
    no base to resolve, the manifest lies outside the repository, or the base has no manifest."""
    try:
        base = resolve_base(args.repo, args.base_ref)
    except CannotRun:
        return None, None
    relative = os.path.relpath(os.path.abspath(args.manifest), os.path.abspath(args.repo))
    if relative.startswith(".."):
        return None, None
    shown = subprocess.run(["git", "-C", args.repo, "show", "%s:%s" % (base, relative.replace(os.sep, "/"))],
                           capture_output=True, text=True)
    if shown.returncode != 0:
        return None, None
    return base, parse_entries(shown.stdout, "%s:%s" % (base, relative))


def judged_entries(args, notice=None):
    """The entries the run judges: the head manifest, unioned with the base's on a pull request. Where they
    differ it says so on `notice` (default stdout). A gate may have several lines, each with its own arguments.
    Every base line runs as the base has it, first and in the base's order, so a reader of a gate's first line
    reads the base's; then each head line no base line accounts for. A head line identical to a base line is
    that line, and the other head lines of a gate stand, in order, for its base lines left over (a line the
    branch changed); only a head line beyond those runs as written."""
    head = parse_manifest(args.manifest)
    base, at_base = base_manifest(args)
    if not at_base:
        return head
    left = list(at_base)  # base lines no head line has claimed yet
    fresh = []
    for line in head:
        if line in left:
            left.remove(line)
        else:
            fresh.append(line)
    added = []
    for line in fresh:
        changed = next((taken for taken in left if taken[0] == line[0]), None)
        if changed:
            left.remove(changed)
        else:
            added.append(line)
    merged = at_base + added
    if merged != head:
        print("vibe-verifier: judged by the manifest at %s where it differs from the head's "
              "(a gate this branch removed or weakened still runs as it was; a gate it added runs too)" % base,
              file=notice or sys.stdout, flush=True)
    return merged


def fail(message):
    print("vibe-verifier: " + message, file=sys.stderr)
    return 2


def read_text(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def run(args):
    entries = judged_entries(args)
    known = available()
    results = [run_entry(args, known, gate, gate_args) for gate, gate_args in entries]
    rows = [(gate, LABEL[code] + (" (soak)" if code == 0 and "--soak" in gate_args else "")
             + (" (%s)" % note if code == 0 and note else "")) for gate, gate_args, code, note in results]
    print("\nvibe-verifier summary")
    for gate, status in rows:
        print("  %-36s %s" % (gate, status))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write("### Vibe Verifier\n\n| Gate | Result |\n|---|---|\n")
            handle.writelines("| `%s` | %s |\n" % row for row in rows)
    codes = [code for _, _, code, _ in results]
    return 2 if 2 in codes else (1 if 1 in codes else 0)


def run_entry(args, known, gate, gate_args):
    """(gate, its arguments, exit code 0, 1 or 2, its note) for one manifest line; an unknown gate is 2."""
    if not re.match(r"^[a-z0-9-]+$", gate) or gate not in known:
        print("vibe-verifier: unknown gate %r (available: %s)" % (gate, ", ".join(known)), file=sys.stderr)
        return gate, gate_args, 2, ""
    command = [sys.executable, os.path.join(GATES, gate.replace("-", "_") + ".py"),
               "--repo", args.repo, "--format", args.format]
    if args.base_ref:
        command += ["--base-ref", args.base_ref]
    print("::group::%s" % gate if args.format == "github" else "== %s ==" % gate, flush=True)
    code, note = gate_process(command + gate_args, os.path.abspath(args.manifest))
    if args.format == "github":
        print("::endgroup::", flush=True)
    return gate, gate_args, code if code in (0, 1) else 2, note


def gate_process(command, manifest):
    """Run one gate: its exit code, and the note it left when its pass checked less than it could
    (`qualify` in gates/_contract.py), else an empty string."""
    handle, note_path = tempfile.mkstemp(prefix="vv-note-")
    os.close(handle)
    try:
        code = subprocess.run(command, env=dict(os.environ, **{repo_rules.MANIFEST: manifest, QUALIFIER: note_path})).returncode
        with open(note_path, encoding="utf-8") as note:
            return code, note.read().strip()
    finally:
        os.unlink(note_path)


def list_command(args):
    print("\n".join(available()))
    return 0


def tool_command(args):
    if not os.path.isfile(os.path.join(NODE_TOOLS, args.name, "package-lock.json")):
        return fail("no toolchain named %r under tools/ (available: %s)" % (
            args.name, ", ".join(sorted(n for n in os.listdir(NODE_TOOLS) if os.path.isfile(os.path.join(NODE_TOOLS, n, "package-lock.json")))) or "none"))
    try:
        print(ensure_node_tool(args.name))
    except CannotRun as error:
        return fail(str(error))
    return 0


def add_run_arguments(parser):
    parser.add_argument("--manifest", default=".vibe-verifier")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--base-ref", default=None)
    parser.add_argument("--format", choices=["text", "github"], default="text")


def add_tool_arguments(parser):
    parser.add_argument("name", help="the toolchain: a directory under tools/ with a package-lock.json")
