"""The reference check, `bin/vibe-verifier reference-check`: a second, fresh model looks at a design reference and the
screenshot a QAE explorer compared it with, and says whether the difference the explorer's FAIL claims is there.

A comparison with a design reference is one model's look (harnesses/qae/prompt.md, `[ref: <key>]`), and a look can
invent a difference: a consumer's run (2026-11-13) failed a criterion on "a close X absent from the reference" when the
reference has the same X. So a FAIL whose one anchor is a comparison step, `qae/ACn.md::step k: compared with reference
<key> -> ...`, is put to a second model before it counts. That model is given the two images, the claim (the FAIL's
own text) and nothing else: no explorer context, no page, no repository, in an empty directory. Its prompt and output
schema are harnesses/reference-check/prompt.md and schema.json. It runs on the proof audit's Codex runner code
(gates/_proof_audit.py's run_codex: tools/codex's lockfile, a ChatGPT-managed login, the user's config ignored).

    reference-check --artifacts DIR   check every such FAIL in DIR/verdict.md and write its record

Each check's record is qae/<ACn>-reference-check.json beside the criterion's step log: the step and reference it is
about, the claim, the digests of both images, and the model's answer or the reason there is none (a usage limit, a
timeout). Records left from before are removed first, so only this run's are in the evidence. The verify job's
acceptance-verdict gate reads them (overturned, below): a FAIL counts as a PASS only when its record matches the FAIL
and the images exactly, the model did not see the claimed difference, and the claim says nothing the two images
cannot settle (a step it could not check, a failed request). Any other record, or none, leaves the FAIL
standing. Exit 0 when every check ran, 2 when one did not.
"""
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import traceback
import types

from _acceptance import DASH, VERDICT_RE, anchors_in, check_lines, named_references
from _contract import CannotRun
from _proof_audit import EFFORTS, MODEL, run_codex
from _proof_table import without_runner_paths

NAME = "reference-check"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(ROOT, "harnesses", "reference-check")
PROMPT = os.path.join(HARNESS, "prompt.md")
SCHEMA = os.path.join(HARNESS, "schema.json")
TOKEN = "acceptance-check"
STEP_TITLE = re.compile(r"^step[ \t]+(?P<k>[0-9]+):")
DEFAULT_TIMEOUT = 600


def record_path(artifacts, item):
    return os.path.join(artifacts, "qae", "%s-reference-check.json" % item)


def digest(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def claim_of(line):
    """What a FAIL line says after `FAIL --`, as written: the claim the second look is asked about."""
    match = VERDICT_RE.match(line)
    return re.sub(r"^[ \t]*" + DASH + r"[ \t]*", "", line[match.end():]).strip() if match else ""


def comparison(item, line):
    """(k, key, claim) when `line` is a FAIL whose one anchor is step k of qae/<item>.md and that step names one
    reference, `key`; None for any other line, which the second look leaves alone."""
    match = VERDICT_RE.match(line)
    if not match or match.group(1).upper() != "FAIL":
        return None
    claim = claim_of(line)
    found = anchors_in(claim)
    if len(found) != 1 or found[0].path != "qae/%s.md" % item:
        return None
    step = STEP_TITLE.match(found[0].title)
    # The reference is named in what the step did; what it saw may say "the reference" in passing.
    named = named_references(found[0].title.split("->", 1)[0])
    if not step or len(named) != 1:
        return None
    return step.group("k"), named[0], claim


def images(artifacts, item, k, key):
    """The reference and the screenshot a comparison is about, as the evidence holds them."""
    return (os.path.join(artifacts, "references", key + ".png"),
            os.path.join(artifacts, "qae", "%s-step-%s.png" % (item, k)))


def overturned(item, line, artifacts):
    """(True, why) when the second look's record for this FAIL refutes it, (False, why) when the FAIL stands for a
    reason the gate should print, and None when the line is not a comparison FAIL."""
    found = comparison(item, line)
    if not found or not artifacts:
        return None
    k, key, claim = found
    path = record_path(artifacts, item)
    if not os.path.isfile(path):
        return False, "no second look at it ran (qae/%s-reference-check.json is not in the evidence)" % item
    try:
        with open(path, encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        return False, "qae/%s-reference-check.json is not JSON" % item
    reference, shot = images(artifacts, item, k, key)
    current = [digest(image) if os.path.isfile(image) else None for image in (reference, shot)]
    if not isinstance(record, dict) or [record.get("step"), record.get("reference"), record.get("claim")] != [k, key, claim] \
            or [record.get("reference_sha256"), record.get("screenshot_sha256")] != current:
        return False, "its second look is about another claim or other images"
    if record.get("error"):
        return False, "its second look did not run: %s" % record["error"]
    answer = record.get("output")
    if not isinstance(answer, dict) or not isinstance(answer.get("confirmed"), bool):
        return False, "its second look gave no answer"
    if answer["confirmed"]:
        return False, "a second look confirmed it: %s" % str(answer.get("reason", ""))[:300]
    if answer.get("other") is not False:
        return False, "its claim also says something the two images cannot settle"
    return True, "a second look at the reference and step %s's screenshot did not see the difference: %s" % (
        k, str(answer.get("reason", ""))[:300])


def render_prompt(claim):
    with open(PROMPT, encoding="utf-8") as handle:
        return handle.read().replace("CLAIM", claim, 1)


def look(args, claim, pair):
    """The model's answer about `claim` and the two images, or the reason there is none."""
    work = tempfile.mkdtemp(prefix="vv-reference-check-")
    try:
        # The model's directory is empty, and a Git repository because the Codex CLI runs only in one. The images are
        # attached, never placed in it.
        empty = os.path.join(work, "empty")
        subprocess.run(["git", "init", "-q", empty], check=True)
        codex = types.SimpleNamespace(**dict(vars(args), repo=empty, grades=None))
        output, error, _ = run_codex(codex, render_prompt(claim), work, SCHEMA, pair)
    except CannotRun as reason:
        output, error = None, str(reason)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return output, error and without_runner_paths(error)


def check_one(args, item, k, key, claim):
    pair = images(args.artifacts, item, k, key)
    missing = [os.path.relpath(image, args.artifacts) for image in pair if not os.path.isfile(image)]
    output, error = (None, "not in the evidence: %s" % ", ".join(missing)) if missing else look(args, claim, pair)
    record = {"step": k, "reference": key, "claim": claim,
              "reference_sha256": None if missing else digest(pair[0]), "screenshot_sha256": None if missing else digest(pair[1]),
              "output": output, "error": error}
    with open(record_path(args.artifacts, item), "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    seen = "not checked: %s" % error if error else "confirmed" if output.get("confirmed") else "not seen, so it does not count"
    print("%s: %s step %s against reference %s: %s" % (NAME, item, k, key, seen), flush=True)
    return error


def validate_options(args):
    if args.model and not MODEL.match(args.model):
        raise CannotRun("--model must be an identifier of letters, digits, dots, underscores and hyphens")
    if args.reasoning_effort and args.reasoning_effort not in EFFORTS:
        raise CannotRun("--reasoning-effort must be one of %s" % ", ".join(EFFORTS))
    if not os.path.isdir(args.artifacts):
        raise CannotRun("artifact directory not found: %s" % args.artifacts)


def comparison_fails(artifacts):
    """[(item, k, key, claim)] for each criterion whose one verdict line is a comparison FAIL."""
    verdict = os.path.join(artifacts, "verdict.md")
    text = open(verdict, encoding="utf-8", errors="replace").read() if os.path.isfile(verdict) else ""
    found = [(item.upper(), comparison(item.upper(), lines[0])) for item, lines in check_lines(text, TOKEN).items()
             if len(lines) == 1]
    return [(item,) + fail for item, fail in found if fail]


def command(args):
    try:
        validate_options(args)
        for stale in glob.glob(record_path(args.artifacts, "*")):
            os.remove(stale)
        fails = comparison_fails(args.artifacts)
        errors = [error for error in (check_one(args, *fail) for fail in fails) if error]
        print("%s: %d comparison FAIL(s) checked, %d not" % (NAME, len(fails) - len(errors), len(errors)), flush=True)
        return 2 if errors else 0
    except CannotRun as reason:
        print("%s could not run: %s" % (NAME, reason), flush=True)
        return 2
    except Exception:
        traceback.print_exc()
        print("%s crashed (traceback above)" % NAME, flush=True)
        return 2


def add_arguments(parser):
    parser.add_argument("--artifacts", required=True, metavar="DIR", help="the explorer's artifact directory, holding "
                        "verdict.md, qae/ and references/")
    parser.add_argument("--model", default="", help="the Codex model (default: the CLI's)")
    parser.add_argument("--reasoning-effort", default="", help="one of %s (default: the model's)" % ", ".join(EFFORTS))
    parser.add_argument("--codex-home", default="", help="the Codex home holding the login (default: $CODEX_HOME, then ~/.codex)")
    parser.add_argument("--codex", default="", help="the Codex CLI to run (default: the one tools/codex pins)")
    parser.add_argument("--sandbox", choices=["read-only", "danger-full-access"], default="read-only",
                        help="Codex's sandbox (default read-only); CI's Linux runners pass danger-full-access, as the "
                        "proof audit does. The model is given no files either way")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="seconds one check may take (default %d)"
                        % DEFAULT_TIMEOUT)
