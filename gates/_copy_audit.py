"""The copy audit's grader, `bin/vibe-verifier copy-audit`: a model grades, cold, whether each sentence and label
a site under test shows is plain English, and gates/copy_audit.py checks every quote it gives against the record.

The record is copy.jsonl, which the QAE harness's browser hook (actions/qae-browser/ui-audit.js) writes beside
ui-audit.jsonl: every block of copy on every page the explorer opened. The model is given each distinct block's
ID, kind and text, and nothing else: not the page, not the repository, not the pull request. Its prompt and output
schema are harnesses/copy-audit/prompt.md and schema.json, the one copy CI and a laptop both run. It runs on the
Codex CLI the proof audit runs (gates/_proof_audit.py's run_codex: tools/codex's lockfile, a ChatGPT-managed login,
the user's config ignored), in an empty directory of its own.

    copy-audit --artifacts DIR                 grade every record in DIR, then run the copy-audit gate on them
    copy-audit --artifacts DIR --grade-only    grade only: CI's explore job, before the evidence is uploaded

A block's ID is a digest of its text, so the gate derives the same IDs from the record. Only pages under the
declared site are graded (--site-file, as the gate's). The blocks are cut into shards of at most --max-blocks,
graded at most --jobs at once, and each record's grades are written beside it as copy-grades.json: per shard, the
IDs it was given, the model's output, and the Codex CLI's own error when the run failed (a usage limit, a timeout,
output that is not JSON), which the gate reports as could not run, never as a pass. Exit 0 when every shard was
graded, 2 when one was not.
"""
import concurrent.futures
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
import types

from _contract import CannotRun
from _proof_audit import EFFORTS, MODEL, run_codex, shards, without_runner_paths
from qae_artifacts import declared_sites
from ui_audit import on_site, read_lines, record_paths

NAME = "copy-audit"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(ROOT, "harnesses", "copy-audit")
PROMPT = os.path.join(HARNESS, "prompt.md")
SCHEMA = os.path.join(HARNESS, "schema.json")
GATE = os.path.join(ROOT, "gates", "copy_audit.py")
RECORD = "copy.jsonl"
GRADES = "copy-grades.json"
DEFAULT_MAX_BLOCKS = 120
DEFAULT_TIMEOUT = 1500  # seconds a shard may take: the explore job's budget is 30 minutes for the explorer


def block_id(text):
    return "C" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]


def blocks(lines, sites):
    """The record's distinct blocks on the declared site, in the order the pages showed them: {id, kind, text}."""
    found = {}
    for line in lines:
        text = str(line.get("text") or "")
        if text and (not sites or any(on_site(str(line.get("page", "")), site) for site in sites)):
            found.setdefault(text, {"id": block_id(text), "kind": line.get("kind") or "label", "text": text})
    return list(found.values())


def render_prompt(cut):
    with open(PROMPT, encoding="utf-8") as handle:
        template = handle.read()
    listed = "\n".join(json.dumps(block, ensure_ascii=False) for block in cut)
    return template.replace("BLOCKS", listed, 1)


def grade(args, number, cut):
    """One shard's envelope: the IDs it was given, the model's output, or the reason there is none."""
    work = tempfile.mkdtemp(prefix="vv-copy-audit-")
    try:
        # The model's directory: empty, and a Git repository because the Codex CLI runs only in one. Its output
        # and log go beside it, in `work`, so a run that wrote into it is still refused.
        empty = os.path.join(work, "empty")
        subprocess.run(["git", "init", "-q", empty], check=True)
        codex = types.SimpleNamespace(**dict(vars(args), repo=empty, grades=None))
        output, error, seconds = run_codex(codex, render_prompt(cut), work, SCHEMA)
    except CannotRun as reason:
        output, error, seconds = None, str(reason), None
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return {"shard": number, "ids": [block["id"] for block in cut], "error": error and without_runner_paths(error),
            "output": output, "seconds": seconds}


def grade_record(args, path, sites):
    """Grade one record and write copy-grades.json beside it. Returns the shards' errors."""
    cuts = shards(blocks(read_lines(path, args.artifacts), sites), args.max_blocks)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(args.jobs, len(cuts) or 1))) as pool:
        graded = list(pool.map(lambda pair: grade(args, *pair), enumerate(cuts, 1)))
    with open(os.path.join(os.path.dirname(path), GRADES), "w", encoding="utf-8") as handle:
        json.dump({"shards": graded}, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    where = os.path.relpath(path, args.artifacts)
    for envelope in graded:
        if envelope["error"]:
            print(("::error title=%s::%s shard %d: %s" if args.format == "github" else "%s: %s shard %d: %s")
                  % (NAME, where, envelope["shard"], envelope["error"]), flush=True)
    print("%s: %s: %d block(s), %d of %d shard(s) graded" % (NAME, where, sum(len(e["ids"]) for e in graded),
                                                            sum(1 for e in graded if not e["error"]), len(graded)), flush=True)
    return [envelope["error"] for envelope in graded if envelope["error"]]


def validate_options(args):
    if args.model and not MODEL.match(args.model):
        raise CannotRun("--model must be an identifier of letters, digits, dots, underscores and hyphens")
    if args.reasoning_effort and args.reasoning_effort not in EFFORTS:
        raise CannotRun("--reasoning-effort must be one of %s" % ", ".join(EFFORTS))
    if args.max_blocks < 1 or args.timeout < 1:
        raise CannotRun("--max-blocks and --timeout must be positive")
    if not os.path.isdir(args.artifacts):
        raise CannotRun("artifact directory not found: %s" % args.artifacts)


def gate_command(args):
    command = [sys.executable, GATE, "--repo", args.repo, "--artifacts", args.artifacts, "--format", args.format]
    command += ["--site-file", args.site_file] if args.site_file else []
    command += ["--base-ref", args.base_ref] if args.base_ref else []
    return command + (["--all"] if args.all else [])


def command(args):
    try:
        validate_options(args)
        sites = declared_sites(args.site_file) if args.site_file else []
        errors = [error for path in record_paths(args.artifacts, RECORD) for error in grade_record(args, path, sites)]
        if args.grade_only:
            return 2 if errors else 0
        return subprocess.run(gate_command(args)).returncode
    except CannotRun as reason:
        print("%s could not run: %s" % (NAME, reason), file=sys.stderr)
        return 2
    except Exception:
        traceback.print_exc()
        print("%s crashed (traceback above)" % NAME, file=sys.stderr)
        return 2


def add_arguments(parser):
    parser.add_argument("--artifacts", required=True, metavar="DIR", help="the directory holding copy.jsonl (at its root, "
                        "or under shard-<n>/ for each of several explorers)")
    parser.add_argument("--site-file", default="", metavar="FILE", help="grade only pages under the URLs in FILE (one a line)")
    parser.add_argument("--grade-only", action="store_true", help="write the grades and stop; do not run the gate")
    parser.add_argument("--repo", default=".", help="for the gate: the checkout whose diff decides which copy is new")
    parser.add_argument("--base-ref", default=None, help="for the gate: the branch the pull request merges into")
    parser.add_argument("--all", action="store_true", help="for the gate: every failing block blocks, not only new copy")
    parser.add_argument("--max-blocks", type=int, default=DEFAULT_MAX_BLOCKS, help="the most blocks one model run grades "
                        "(default %d)" % DEFAULT_MAX_BLOCKS)
    parser.add_argument("--jobs", type=int, default=4, help="shards graded at once (default 4)")
    parser.add_argument("--model", default="", help="the Codex model (default: the CLI's)")
    parser.add_argument("--reasoning-effort", default="", help="one of %s (default: the model's)" % ", ".join(EFFORTS))
    parser.add_argument("--codex-home", default="", help="the Codex home holding the login (default: $CODEX_HOME, then ~/.codex)")
    parser.add_argument("--codex", default="", help="the Codex CLI to run (default: the one tools/codex pins)")
    parser.add_argument("--sandbox", choices=["read-only", "danger-full-access"], default="read-only",
                        help="Codex's sandbox (default read-only); CI's Linux runners pass danger-full-access, as the "
                        "proof audit does. The model is given no files either way")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="seconds one shard may take (default %d)" % DEFAULT_TIMEOUT)
    parser.add_argument("--format", choices=["text", "github"], default="text")
