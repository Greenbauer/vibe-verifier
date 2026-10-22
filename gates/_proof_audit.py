"""The proof audit's grader, `bin/vibe-verifier proof-audit`: a model grades, cold, whether this repository's
tests prove each requirement the pull request's Ticket sections define, and gates/proof_audit.py checks
every quote it gives against the repository.

The requirements are the `[req: ID]` items of each section a `Ticket: <plan>#<anchor>` line of the body
links, read at the merge base exactly as ticket-coverage reads them. The model is given those IDs with
their wording and where the plan defines them, and the repository's tree; never the body, and never the
body's `[proof:]` map: it finds the proving tests itself. Its prompt and output schema are
harnesses/proof-audit/prompt.md and schema.json, the one copy CI and a laptop both run. It runs on the
Codex CLI from tools/codex's lockfile, under a ChatGPT-managed login (CODEX_HOME, default ~/.codex), with
the user's config ignored, so a laptop grades with the settings CI uses, and a read-only sandbox (--sandbox;
CI's Linux runners cannot start Codex's sandbox and pass danger-full-access, as the QAE explorer does).

    proof-audit --body FILE                       plan, grade every shard, run the gate: CI's exit code
    proof-audit --body FILE --plan OUT            write the plan (the requirements, split into shards)
    proof-audit --requirements PLAN --shard K     grade shard K of a plan into --grades DIR

A shard is at most --max-ids requirements (default 10), so each model run stays well inside one QAE job's
time budget, and CI grades the shards in parallel. Grading one shard writes `<grades>/<K>.json`: the IDs it
was given, the model's output, and the Codex CLI's own error when the run failed (a usage limit, a
timeout, output that is not JSON, a working tree the run changed), which the gate reports as could not
run, never as a pass. Exit 0 when the shard was graded, 2 when it was not.
"""
import concurrent.futures
import importlib.util
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback

from _contract import CannotRun, git, resolve_base
from _ticket import Unreadable, links
import ticket_coverage

NAME = "proof-audit"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(ROOT, "harnesses", "proof-audit")
PROMPT = os.path.join(HARNESS, "prompt.md")
SCHEMA = os.path.join(HARNESS, "schema.json")
GATE = os.path.join(ROOT, "gates", "proof_audit.py")
DEFAULT_MAX_IDS = 10
DEFAULT_TIMEOUT = 3000  # seconds: inside a 55-minute job with its checkout and install
PLACEHOLDER = re.compile(r"\b(REQUIREMENTS|SECTIONS)\b")
EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def merge_base(repo, base_ref):
    return git(repo, "merge-base", resolve_base(repo, base_ref), "HEAD").strip()


def requirements(repo, base_ref, body):
    """(requirements, notes): every requirement ID the body's Ticket links define at the merge base, in
    plan order, each {id, text, plan, line, section, heading}; notes say why there is nothing to audit
    (no Ticket line, or a linked section with no IDs). An unreadable link, file or anchor is CannotRun."""
    try:
        found = links(body)
    except Unreadable as error:
        raise CannotRun(str(error))
    if not found:
        return [], ["no ticket linked"]
    base = merge_base(repo, base_ref)
    result, notes, seen = [], [], set()
    for path, anchor in found:
        lines, start, level = ticket_coverage.linked_section(repo, base, path, anchor)
        defined = ticket_coverage.section_ids(lines, start, level)
        if not defined:
            notes.append("%s#%s: ticket section has no requirement IDs; nothing audited" % (path, anchor))
        for rid, number, text in defined:
            if rid not in seen:
                seen.add(rid)
                result.append({"id": rid, "text": text, "plan": path, "line": number,
                               "section": "%s#%s" % (path, anchor), "heading": lines[start].lstrip("#").strip()})
    return result, notes


def shards(items, max_ids):
    """The requirements in plan order, cut into the fewest contiguous runs of at most max_ids, as even as
    they can be, so neighbouring requirements (which often share a test file) are graded together."""
    if not items:
        return []
    count = math.ceil(len(items) / max_ids)
    size, extra = divmod(len(items), count)
    cuts, at = [], 0
    for index in range(count):
        width = size + (1 if index < extra else 0)
        cuts.append(items[at:at + width])
        at += width
    return cuts


def render_prompt(items):
    """harnesses/proof-audit/prompt.md with this shard's requirements and their sections filled in, in one
    pass, so a requirement whose wording holds a placeholder's name is left as written."""
    with open(PROMPT, encoding="utf-8") as handle:
        template = handle.read()
    listed = "\n".join("- %s (%s:%d): %s" % (item["id"], item["plan"], item["line"], item["text"]) for item in items)
    sections = []
    for item in items:
        line = "- %s (heading \"%s\")" % (item["section"], item["heading"])
        if line not in sections:
            sections.append(line)
    values = {"REQUIREMENTS": listed, "SECTIONS": "\n".join(sections)}
    return PLACEHOLDER.sub(lambda match: values[match.group(1)], template)


def check_login(home):
    """The Codex home a run uses, when it holds a ChatGPT-managed login with a refresh token (the same check
    actions/qae-codex makes before the explorer), else CannotRun with the seeding instruction."""
    path = os.path.join(home, "auth.json")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        raise CannotRun("no Codex login at %s: seed it once, as this user: CODEX_HOME=%s codex login --device-auth "
                        "(harnesses/qae/README.md)" % (path, home))
    if data.get("auth_mode") != "chatgpt" or not (data.get("tokens") or {}).get("refresh_token"):
        raise CannotRun("%s is not a ChatGPT-managed login with a refresh token" % path)
    return home


def collector():
    """actions/usage/collect.py, whose reader of the Codex CLI's JSONL events this reuses: it keeps the
    CLI's own `error` and `turn.failed` messages (a usage limit among them) and never the model's text."""
    spec = importlib.util.spec_from_file_location("vv_usage_collect", os.path.join(ROOT, "actions", "usage", "collect.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tree_state(repo, grades):
    """git status of the checkout, leaving out the grades directory when it is inside it: another shard of
    the same run writes its grades there while this one runs."""
    inside = os.path.relpath(os.path.abspath(grades), os.path.abspath(repo)) if grades else ".."
    skip = [] if inside == ".." or inside.startswith(".." + os.sep) else [":(exclude)%s" % inside.replace(os.sep, "/")]
    return subprocess.run(["git", "-C", repo, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", "."] + skip,
                          capture_output=True).stdout


def codex_command(args, prompt, last):
    codex = args.codex
    if not codex:
        from _tools import ensure_node_tool
        codex = os.path.join(ensure_node_tool("codex"), "node_modules", ".bin", "codex")
    command = [codex, "exec", "--json", "--ephemeral", "--ignore-user-config", "--ignore-rules",
               "--sandbox", args.sandbox, "-c", 'approval_policy="never"', "-c", 'cli_auth_credentials_store="file"',
               "--cd", os.path.abspath(args.repo), "--output-schema", SCHEMA, "-o", last]
    if args.model:
        command += ["--model", args.model]
    if args.reasoning_effort:
        command += ["-c", 'model_reasoning_effort="%s"' % args.reasoning_effort]
    return command + [prompt]


def run_codex(args, prompt, work):
    """(output, error, seconds): the model's final message parsed as JSON, or the reason there is none."""
    last = os.path.join(work, "last.json")
    errors = os.path.join(work, "stderr.log")
    home = args.codex_home or os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    env = dict(os.environ, CODEX_HOME=check_login(home))
    before, started = tree_state(args.repo, args.grades), time.monotonic()
    with open(errors, "wb") as stderr:
        child = subprocess.Popen(codex_command(args, prompt, last), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=stderr, env=env, start_new_session=True)
        expired = threading.Event()

        def stop():
            expired.set()
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

        timer = threading.Timer(args.timeout, stop)
        timer.start()
        try:
            _, _, _, last_error = collector().stream_codex(child.stdout)
            code = child.wait()
        finally:
            timer.cancel()
            child.stdout.close()
    seconds = round(time.monotonic() - started, 1)
    if expired.is_set():
        return None, "the grader ran past --timeout (%d seconds)" % args.timeout, seconds
    if code != 0:
        with open(errors, encoding="utf-8", errors="replace") as handle:
            lines = [line.strip() for line in handle.read().splitlines() if line.strip()] or ["no output"]
        # A crashed CLI's stderr ends with a stack ("at onErrorNT (...)") and Node's version banner; the error is
        # the last line that starts with an error's name ("Error: spawn ... ENOENT"), else one that mentions one.
        named = [line for line in lines if re.match(r"\w*Error\b", line)]
        tail = (named or [line for line in lines if re.search(r"error", line, re.I)] or lines)[-1]
        return None, "Codex ended with an error: %s" % (last_error or "exit %d (%s)" % (code, tail[:300])), seconds
    if tree_state(args.repo, args.grades) != before:
        return None, "the grader changed the working tree, which it may only read", seconds
    try:
        with open(last, encoding="utf-8") as handle:
            return json.load(handle), None, seconds
    except (OSError, ValueError) as error:
        return None, "the grader's final message is not JSON: %s" % error, seconds


def grade(args, number, items):
    """Grade one shard and write its envelope, `<grades>/<number>.json`. Returns the envelope's error."""
    work = tempfile.mkdtemp(prefix="vv-proof-audit-")
    try:
        output, error, seconds = run_codex(args, render_prompt(items), work)
    except CannotRun as reason:
        output, error, seconds = None, str(reason), None
    # The error reaches the pull request's comment, and this runner is not the one that renders it.
    envelope = {"shard": number, "ids": [item["id"] for item in items], "error": error and without_runner_paths(error),
                "output": output, "seconds": seconds}
    os.makedirs(args.grades, exist_ok=True)
    with open(os.path.join(args.grades, "%d.json" % number), "w", encoding="utf-8") as handle:
        json.dump(envelope, handle, indent=2)
        handle.write("\n")
    where = "shard %d (%s)" % (number, ", ".join(envelope["ids"]))
    if error:
        print(("::error title=%s::%s: %s" if args.format == "github" else "%s: %s: %s") % (NAME, where, error), flush=True)
    else:
        print("%s: graded %s in %s seconds" % (NAME, where, seconds), flush=True)
    return error


def read_plan(path):
    try:
        with open(path, encoding="utf-8") as handle:
            plan = json.load(handle)
        return [list(cut) for cut in plan["shards"]]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise CannotRun("cannot read the plan %s: %s" % (path, error))


def plan_of(args):
    try:
        with open(args.body, encoding="utf-8") as handle:
            body = handle.read()
    except OSError as error:
        raise CannotRun("--body cannot be read: %s" % error)
    items, notes = requirements(args.repo, args.base_ref, body)
    for note in notes:
        print("%s: %s" % (NAME, note), flush=True)
    return shards(items, args.max_ids)


def validate_options(args):
    if args.model and not MODEL.match(args.model):
        raise CannotRun("--model must be an identifier of letters, digits, dots, underscores and hyphens")
    if args.reasoning_effort and args.reasoning_effort not in EFFORTS:
        raise CannotRun("--reasoning-effort must be one of %s" % ", ".join(EFFORTS))
    if args.max_ids < 1 or args.timeout < 1:
        raise CannotRun("--max-ids and --timeout must be positive")


def audit(args):
    """Plan, grade every shard (at most --jobs at once), then run the gate on what was graded."""
    cuts = plan_of(args)
    args.grades = args.grades or tempfile.mkdtemp(prefix="vv-proof-grades-")
    print("%s: %d requirement(s) in %d shard(s); grades in %s" % (NAME, sum(map(len, cuts)), len(cuts), args.grades), flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(args.jobs, len(cuts) or 1))) as pool:
        list(pool.map(lambda pair: grade(args, *pair), enumerate(cuts, 1)))
    table = os.path.join(args.grades, "table.md")
    command = [sys.executable, GATE, "--repo", args.repo, "--body", args.body, "--grades", args.grades,
               "--table", table, "--format", args.format]
    if args.base_ref:
        command += ["--base-ref", args.base_ref]
    for glob in args.require_ticket:
        command += ["--require-ticket", glob]
    code = subprocess.run(command).returncode
    if os.path.isfile(table):
        with open(table, encoding="utf-8") as handle:
            print("\n" + handle.read())
    return code


def command(args):
    try:
        validate_options(args)
        if args.plan:
            cuts = plan_of(args)
            with open(args.plan, "w", encoding="utf-8") as handle:
                json.dump({"shards": cuts}, handle)
            print("requirements: %d" % sum(map(len, cuts)))
            print("shards: [%s]" % ",".join(str(number) for number in range(1, len(cuts) + 1)))
            return 0
        if args.shard is not None:
            if not args.requirements or not args.grades:
                raise CannotRun("--shard needs --requirements (a plan) and --grades")
            cuts = read_plan(args.requirements)
            if not 1 <= args.shard <= len(cuts):
                raise CannotRun("the plan has %d shard(s), not a shard %d" % (len(cuts), args.shard))
            return 2 if grade(args, args.shard, cuts[args.shard - 1]) else 0
        if not args.body:
            raise CannotRun("give --body (the pull request's body, outside the repository)")
        return audit(args)
    except CannotRun as reason:
        print("%s could not run: %s" % (NAME, reason), file=sys.stderr)
        return 2
    except Exception:
        traceback.print_exc()
        print("%s crashed (traceback above)" % NAME, file=sys.stderr)
        return 2


def add_arguments(parser):
    parser.add_argument("--repo", default=".")
    parser.add_argument("--base-ref", default=None, help="the branch the pull request merges into (default: "
                        "$VIBE_VERIFIER_BASE_REF, $GITHUB_BASE_REF, main, master)")
    parser.add_argument("--body", metavar="FILE", help="the pull request's body: its Ticket lines name the requirements. "
                        "Keep it outside the repository, which the model reads")
    parser.add_argument("--plan", metavar="OUT", help="write the plan (the requirements cut into shards) and stop")
    parser.add_argument("--requirements", metavar="PLAN", help="with --shard, the plan --plan wrote")
    parser.add_argument("--shard", type=int, metavar="K", help="grade only shard K of --requirements")
    parser.add_argument("--grades", metavar="DIR", help="where each shard's grades are written (default: a new temporary directory)")
    parser.add_argument("--max-ids", type=int, default=DEFAULT_MAX_IDS, help="the most requirements one model run grades "
                        "(default %d)" % DEFAULT_MAX_IDS)
    parser.add_argument("--jobs", type=int, default=4, help="shards graded at once (default 4)")
    parser.add_argument("--model", default="", help="the Codex model (default: the CLI's)")
    parser.add_argument("--reasoning-effort", default="", help="one of %s (default: the model's)" % ", ".join(EFFORTS))
    parser.add_argument("--codex-home", default="", help="the Codex home holding the login (default: $CODEX_HOME, then ~/.codex)")
    parser.add_argument("--codex", default="", help="the Codex CLI to run (default: the one tools/codex pins)")
    parser.add_argument("--sandbox", choices=["read-only", "danger-full-access"], default="read-only",
                        help="Codex's sandbox (default read-only). On a Linux runner in an unprivileged container Codex's "
                        "sandbox (bubblewrap) cannot start and every read fails, so CI passes danger-full-access, as the QAE "
                        "explorer does; a run that changed the working tree is refused either way")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="seconds one shard may take (default %d)" % DEFAULT_TIMEOUT)
    parser.add_argument("--require-ticket", action="append", default=[], metavar="GLOB",
                        help="passed to the gate: a pull request that changes a file matching GLOB (repeatable) must link "
                        "a Ticket section or say why none applies on a `No ticket: <why>` line")
    parser.add_argument("--format", choices=["text", "github"], default="text")


# The per-ID table proof_audit.py writes, which the action posts as the pull request's proof-audit comment.
def proof_summary(grade):
    return "; ".join("%s: %s" % (case["case"], ", ".join(proof_cite(proof) for proof in case["proof"]))
                     for case in grade["cases"])


def proof_cite(proof):
    """`file:line` (test), and the line the grader cited when the gate found the quote elsewhere in that test."""
    moved = "; cited :%d" % proof["cited_line"] if "cited_line" in proof else ""
    return "`%s:%d` (%s%s)" % (proof["file"], proof["line"], proof["test"].strip(), moved)


def without_runner_paths(text):
    """`text` with this runner's own folders (workspace, temp, home) dropped from every path that starts with one:
    only where a path starts, so a repository path or a route that merely contains the same name stays."""
    roots = {os.environ.get(name, "").rstrip("/") for name in ("GITHUB_WORKSPACE", "RUNNER_TEMP", "HOME", "TMPDIR")}
    for root in sorted(filter(None, roots), key=len, reverse=True):
        text = re.sub(r"(?<![\w./~-])" + re.escape(root) + "/", "", text)
    return text


def cell(text, limit=600):
    """One line of the comment, for a person: in text the gate did not write (a CLI's error, a grade) a URL goes to
    the job log, and the rendering runner's folders are dropped from a path (docs/comments.md)."""
    text = re.sub(r"https?://\S+", "(link in the job log)", " ".join(str(text).split()).replace("|", "\\|"))
    text = without_runner_paths(text)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def table(rows, note=None):
    proven = sum(1 for _, status, _ in rows if status != "UNPROVEN")
    lines = ["## Proof audit: %s" % cell(note or "%d of %d requirements proven or excluded" % (proven, len(rows))), ""]
    if rows:
        lines += ["| ID | Verdict | Proof, or what is missing |", "|---|---|---|"]
        lines += ["| %s | %s | %s |" % (rid, status, cell(detail)) for rid, status, detail in rows]
    return "\n".join(lines) + "\n"


def write_table(path, text):
    if path:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
