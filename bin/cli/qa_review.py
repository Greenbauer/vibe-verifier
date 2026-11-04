"""`qa-review`: the QAE's one review comment on a pull request, posted or edited in place. Its state comes from what
the workflow's jobs and gates concluded, never from the explorer's prose."""
import argparse
import json
import os
import re
import sys

from _acceptance import (body_criteria as read_criteria, check_lines, declares_none, said, ticket_items, ticket_proofs,
                         PROOF_TOKEN, verdict)
from _contract import CannotRun, resolve_base
from _features import map_directory
from _pin_bump import CannotRead, gh_json
from _probe import probes
from _proof_audit import cell
from _retry import retries
from _verify_edits import verify_edits
from cli.features import selection_options
from cli.qa_limit import describe_limits
from cli.runner import fail, judged_entries, read_text

# The QAE's review comment: one per pull request, found again by this first line and by the identity a
# workflow's GITHUB_TOKEN posts as, so a comment anyone else writes is never edited.
REVIEW_MARKER = "<!-- vibe-verifier:qa-review -->"
REVIEW_AUTHOR = "github-actions[bot]"
REVIEW_HEADINGS = {
    "passed": "## QA review: passed ✅",
    "changes-needed": "## QA review: changes needed ❌",
    "not-required": "## QA review: not required ✅",
    "carried": "## QA review: passed on an earlier head ✅",
    "could-not-run": "## QA review: could not run ⚠️",
    "usage-limit": "## QA review: not walked, model usage limit ⚠️",
}


EXPLORE_DONE = ("success", "skipped")


def review_state(args, limits=None):
    """What the workflow concluded, in order: a pull request that needs no check, an explore job that
    did not finish, then the gates' own outcome. Gates that never ran (an earlier verify step failed)
    judged nothing, so that is could-not-run too, never a refusal. A skipped explore job found no
    criterion to walk: the verify job's first step fails when the criteria job that decided it did not
    finish, so its gates never run and that case lands on could-not-run too. A failed explore job whose
    every explorer hit a model usage limit (`limits`, the qa-limit step's, from explorer_limits) is usage-limit."""
    if args.not_required:
        return "carried" if args.carried_from else "not-required"
    if args.explore_result == "failure" and limits:
        return "usage-limit"
    if args.explore_result not in EXPLORE_DONE or args.gates_outcome not in ("success", "failure"):
        return "could-not-run"
    return "passed" if args.gates_outcome == "success" else "changes-needed"


def review_criteria(criteria_text, verdict_text, ticket_text="", edits=None, retried=None):
    ticket, problem = ticket_items(ticket_text)
    wanted = ([] if declares_none(criteria_text) and (ticket or problem) else read_criteria(criteria_text)) + ticket
    rows = ["The ticket's criteria could not be read: %s." % problem] if problem else []
    if not wanted:
        return rows or ["The pull request lists no acceptance criteria. Add a `## Acceptance criteria` list, or "
                        "`- None: <why>` when nothing on the site changes."]
    return rows + criterion_rows(wanted, criteria_text, verdict_text, retried) + feature_rows(verdict_text, edits)


def criterion_rows(wanted, criteria_text, verdict_text, retried):
    """One row per criterion: what the explorer said of it, or the test the pull request proves a ticket's with."""
    lines = check_lines(verdict_text, "acceptance-check")
    proofs = ticket_proofs(criteria_text)
    rows = []
    for item, wording in wanted:
        source = " (from the ticket)" if item.startswith("TC") else ""
        proof = proofs.get(item.casefold()) if item.startswith("TC") else None
        said_by = ("not walked, the pull request proves it with a test: %s" % said(proof[0], PROOF_TOKEN) if proof
                   else explorer_said(lines.get(item.casefold(), [])))
        # A step the explorer took again after an environment error, and its invalid-input probe a 400 or 422 answered,
        # as the qae-artifacts gate judged them (`retried`, {criterion: [what it found]} from retry_notes): a PASS on a
        # retry or a refused probe says so, and why.
        if (retried or {}).get(item):
            said_by += " (%s)" % "; ".join(retried[item])
        rows.append("- %s%s, %s: %s" % (item, source, said_by, wording))
    return rows


def feature_rows(verdict_text, edits):
    """A feature re-walk (docs/feature-map.md) adds one line per feature; which features were required is
    the gate's to decide, so this lists what the explorer wrote and the gate's log names any missing.
    A re-walk is partial by design, so the states the explorer says it left unwalked are listed too, and so is
    what it found on the base branch's site for a state that failed (whether that counts is the gate's to say).
    The steps that judged a re-walk are the pull request's own copy, so a row says when it changed them (`edits`,
    {id: note} from feature_edits); a str is why that could not be read."""
    skipped, known = check_lines(verdict_text, "regression-skip"), check_lines(verdict_text, "regression-known")
    rows = []
    for fid, carried in check_lines(verdict_text, "regression-check").items():
        unwalked = "; ".join(said(line, "regression-skip") for line in skipped.get(fid, []))
        on_main = "; ".join(said(line, "regression-known") for line in known.get(fid, []))
        edited = edits.get(fid) if isinstance(edits, dict) else None
        rows.append("- Feature `%s` re-walked, %s%s%s%s" % (fid, explorer_said(carried),
                                                           on_main and ", and says the base branch fails the same way: %s" % on_main,
                                                           unwalked and " (not walked: %s)" % unwalked,
                                                           ". Note: %s" % edited if edited else ""))
    if isinstance(edits, str):
        rows.append("- Whether this pull request changed the Verify section of a re-walked feature could not be read: %s."
                    % edits.rstrip("."))
    return rows


def feature_edits(args, fids):
    """{id: note} for the re-walked features `fids` (case-folded, as the verdict names them) whose Verify section the
    pull request changed (gates/_verify_edits.py), from the map the QAE manifest's acceptance-verdict line names and
    the base, as the gate reads them in the checkout; why, as a str, when that cannot be read; {} when the manifest
    turns no re-walk on."""
    if not fids or not os.path.isfile(args.manifest):
        return {}
    try:
        options = selection_options(argparse.Namespace(manifest=args.manifest, repo=args.checkout, base_ref=None))
        if not options or not options.features:
            return {}
        names = {name[:-3].casefold(): name[:-3] for name in
                 os.listdir(os.path.join(args.checkout, map_directory(options.features))) if name.endswith(".md")}
        notes = verify_edits(args.checkout, options.features, [names[fid] for fid in fids if fid in names],
                             resolve_base(args.checkout))
    except SystemExit:
        return "the acceptance-verdict line in %s cannot be read" % args.manifest
    except (CannotRun, OSError, ValueError) as error:
        return str(error)
    return {fid.casefold(): note for fid, note in notes.items()}


def retry_notes(args):
    """{criterion: [what the qae-artifacts gate found of each step the explorer retried, and of each invalid-input probe
    a 400 or 422 answered]}, read from the run's evidence (gates/_retry.py, gates/_probe.py) with the changed paths the
    manifest's qae-artifacts line gives that gate, judged from the base as the gate's line is: without them the gate
    counts no URL as the environment, and neither does this."""
    changed = None
    if os.path.isfile(args.manifest):
        try:
            lines = [gate_args for gate, gate_args in judged_entries(argparse.Namespace(
                manifest=args.manifest, repo=args.checkout, base_ref=None), notice=sys.stderr) if gate == "qae-artifacts"]
        except (SystemExit, CannotRun, OSError, ValueError):
            lines = []
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--changed-files")
        changed = parser.parse_known_args(lines[0])[0].changed_files if lines else None
    notes = {}
    for found in retries(args.artifacts, changed) + probes(args.artifacts):
        notes.setdefault(found.item, []).append(found.describe())
    return notes


def explorer_said(carried):
    return "no verdict" if len(carried) != 1 else "explorer says PASS" if verdict(carried[0]) == "PASS" else "explorer says FAIL"


def usage_limit_lines(head, limits):
    """The usage-limit review's text: the walk did not run, which limit each explorer hit and when it resets."""
    return (["The walk did not run on %s: every explorer hit its model usage limit, so nothing was checked in a "
             "browser. The Acceptance criteria check passes with a warning, and this head is recorded as limited, "
             "never as a walk:" % head, ""]
            + ["- %s" % line for line in describe_limits(limits)]
            + ["", "Rerun this workflow after the reset: the next walk checks every criterion on the head it runs on."])


def review_body(state, args, limits=None):
    # A link that says what it is, never a hash in the text (docs/comments.md).
    head = "[this commit](%s/commit/%s)" % (args.run_url.split("/actions/runs/")[0], args.head)
    lines = [REVIEW_MARKER, REVIEW_HEADINGS[state], ""]
    if state == "not-required":
        lines.append("No browser check on %s: %s." % (head, args.not_required.rstrip(".")))
    elif state == "carried":
        lines.append("No new browser check on %s: %s. The criteria were not edited since." % (head, args.not_required.rstrip(".")))
    elif state == "usage-limit":
        lines += usage_limit_lines(head, limits)
    elif state == "could-not-run":
        stopped = ("The explore job ended `%s`" % args.explore_result if args.explore_result not in EXPLORE_DONE
                   else "The verify job stopped before its gates ran")
        lines.append("%s on %s, so nothing was checked. Its log has the cause; rerun the workflow once it "
                     "is fixed." % (stopped, head))
    else:
        lines += walked_lines(state, args, head)
    lines += ["", "[The run and its evidence](%s)" % args.run_url]
    if state == "usage-limit":
        # This head's record that it was not walked, shaped like the review harness's `limited` receipt.
        lines.append("<!-- qa-review: %s -- limited -- run %s -->" % (args.head, args.run_id))
    return "\n".join(lines) + "\n"


def walked_lines(state, args, head):
    """The review of a walk the gates judged: passed or changes needed, then each criterion's row."""
    lines = ["Every criterion on %s has a PASS anchored to a step the explorer logged, and the "
             "evidence gate found nothing." % head if state == "passed" else
             "The QA gates refused %s. Their findings are in the log of the job that posted this." % head, ""]
    verdict = read_text(args.verdict) if args.verdict and os.path.isfile(args.verdict) else ""
    ticket = read_text(args.ticket) if args.ticket and os.path.isfile(args.ticket) else ""
    edits = feature_edits(args, list(check_lines(verdict, "regression-check")))
    return (lines + review_criteria(read_text(args.criteria), verdict, ticket, edits, retry_notes(args))
            + soak_lines(args.soak_findings))


def soak_lines(path):
    """Each finding a gate under --soak reported without failing, from the file the gates step of this job wrote
    (_contract.SOAK_FINDINGS): written for the builder and the reviewer, who read this comment, not the job log."""
    if not path or not os.path.isfile(path):
        return []
    found, unreadable = [], 0
    for line in read_text(path).splitlines():
        try:
            item = json.loads(line) if line.strip() else None
        except ValueError:
            item = None
        if isinstance(item, dict) and item.get("gate") and item.get("message"):
            found.append(item)
        elif line.strip():
            unreadable += 1  # a gate stopped mid-record; it printed its findings to the job log first (_contract.report)
    if not found and not unreadable:
        return []
    lines = ["", "**Not yet blocking.** These gates run in soak, so their findings do not fail this check yet. A gate "
             "that compares with the base lists only what this pull request added; any other lists everything it "
             "found on the pages or files it read. Fix the ones this pull request added before asking for review.", ""]
    lines += ["- `%s`: %s" % (item["gate"], soak_text(item)) for item in found]
    if unreadable:
        lines.append("- %d record line(s) could not be read; the job log has every soak finding." % unreadable)
    return lines


def soak_text(item):
    """A finding as a person reads it: a page by its path, not the preview's address. A gate's count of the other
    pages or rows a finding was also on is dropped: the comment lists findings, and the run's evidence (the gate's
    record) has each place."""
    text = re.sub(r"https?://[^/\s\"'()]+(/[^\s\"'()]*)?", lambda m: m.group(1) or "/", item["message"])
    text = re.sub(r" \(and (?:on )?\d+ more(?: page\(s\)| like it)?\)", "", text)
    where = item["path"] + (":%d" % item["line"] if item.get("line") else "") + ": " if item.get("path") else ""
    return cell(where + text)


def review_comment_ids(repo, pr):
    ids, page = [], 1
    while True:
        batch = gh_json("repos/%s/issues/%s/comments?per_page=100&page=%d" % (repo, pr, page))
        ids += [c["id"] for c in batch if c["user"]["login"] == REVIEW_AUTHOR and c["body"].startswith(REVIEW_MARKER)]
        if len(batch) < 100:
            return ids
        page += 1


def qa_review_command(args):
    try:
        limits = json.loads(args.limits) if args.limits else None
        state = review_state(args, limits)
        body = review_body(state, args, limits)
        found = review_comment_ids(args.repo, args.pr)
        if found:
            gh_json("repos/%s/issues/comments/%s" % (args.repo, found[0]), method="PATCH", body={"body": body})
        else:
            gh_json("repos/%s/issues/%s/comments" % (args.repo, args.pr), method="POST", body={"body": body})
    except (OSError, CannotRead, ValueError) as error:
        return fail("could not post the QA review: %s" % error)
    print("%s the QA review: %s" % ("edited" if found else "posted", state))
    return 0


def add_arguments(parser):
    parser.add_argument("--repo", required=True, metavar="OWNER/NAME")
    parser.add_argument("--pr", required=True, metavar="N")
    parser.add_argument("--head", required=True, metavar="SHA", help="the head commit the run judged")
    parser.add_argument("--run-url", required=True, metavar="URL")
    parser.add_argument("--criteria", required=True, metavar="FILE", help="the PR body the run read")
    parser.add_argument("--verdict", metavar="FILE", help="the explorers' verdict, as the verify job read it from "
                        "the run's evidence, when there is one")
    parser.add_argument("--ticket", metavar="FILE", help="the ticket's criteria the run read, when the workflow supplies them")
    parser.add_argument("--explore-result", required=True, help="the explore job's result (success, skipped, failure, cancelled)")
    parser.add_argument("--gates-outcome", default="", help="the gates step's outcome (success, failure, skipped)")
    parser.add_argument("--not-required", default="", metavar="REASON", help="why no browser check is needed; empty when one is")
    parser.add_argument("--carried-from", default="", metavar="SHA", help="with --not-required, the head whose passing "
                        "walk this head carries (the criteria action's carried-from output)")
    parser.add_argument("--manifest", default=".vibe-verifier-qae", help="the QAE manifest: its acceptance-verdict line's "
                        "--features names the map whose Verify sections a re-walked feature's row compares with the base")
    parser.add_argument("--checkout", default=".", metavar="DIR", help="the pull request's checkout, with the base's history")
    parser.add_argument("--artifacts", default="qae-artifacts", metavar="DIR", help="the run's evidence: a step the "
                        "explorer retried after an environment error is named on its criterion's row")
    parser.add_argument("--run-id", default="", metavar="ID", help="the workflow run's id, named in a limited head's record")
    parser.add_argument("--soak-findings", default="", metavar="FILE", help="the findings the gates step's soak gates "
                        "reported without failing (one JSON object a line); listed under Not yet blocking")
    parser.add_argument("--limits", default="", metavar="JSON", help="the qa-limit step's limits: when it found every "
                        "explorer hit its model usage limit, the review says the walk did not run and when each resets")
