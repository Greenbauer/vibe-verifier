"""The QA review's walk tables (`qa-review`, bin/cli/qa_review.py): one row per criterion and per re-walked feature,
then the step each explorer sentence was anchored to, collapsed. Rows are a table with a status mark, never checkboxes
(docs/comments.md)."""
import re

from _acceptance import (body_criteria as read_criteria, check_lines, declares_none, said, ticket_items, ticket_proofs,
                         DASH, PROOF_TOKEN, verdict)
from _proof_audit import cell

# A row's mark: the explorer's PASS or FAIL, a criterion it gave no single verdict, and a ticket criterion a test proves.
MARKS = {"PASS": "\u2705", "FAIL": "\u274c", None: "\u26a0\ufe0f"}
TESTED = "\U0001f9ea"


def review_criteria(criteria_text, verdict_text, ticket_text="", edits=None, retried=None):
    """The walk's tables: one row per criterion, then one per re-walked feature, then the step each result was
    anchored to, collapsed (a reviewer opens it to find the screenshot in the run's evidence)."""
    ticket, problem = ticket_items(ticket_text)
    wanted = ([] if declares_none(criteria_text) and (ticket or problem) else read_criteria(criteria_text)) + ticket
    lines = ["The ticket's criteria could not be read: %s." % problem, ""] if problem else []
    if not wanted:
        return lines or ["The pull request lists no acceptance criteria. Add a `## Acceptance criteria` list, or "
                         "`- None: <why>` when nothing on the site changes."]
    rows, sources = criterion_rows(wanted, criteria_text, verdict_text, retried)
    features, feature_sources = feature_rows(verdict_text, edits)
    sources += feature_sources
    if sources:
        features += ["", "<details>", "<summary>Where each result came from</summary>", ""] + sources + ["", "</details>"]
    return lines + rows + features


def criterion_rows(wanted, criteria_text, verdict_text, retried):
    """The criteria table, one row each: what the explorer saw, or the test the pull request proves a ticket's with;
    and the step each explorer sentence was anchored to."""
    lines = check_lines(verdict_text, "acceptance-check")
    proofs = ticket_proofs(criteria_text)
    rows, sources = ["| | Criterion | What the browser showed |", "|:-:|---|---|"], []
    for item, wording in wanted:
        source = " (from the ticket)" if item.startswith("TC") else ""
        proof = proofs.get(item.casefold()) if item.startswith("TC") else None
        if proof:
            mark, shown, anchor = TESTED, "Not walked: the pull request proves it with a test, %s" % said(proof[0], PROOF_TOKEN), None
        else:
            mark, shown, anchor = explored(lines.get(item.casefold(), []), "acceptance-check")
        # A step the explorer took again after an environment error, and its invalid-input probe a 400 or 422 answered,
        # as the qae-artifacts gate judged them (`retried`, {criterion: [what it found]} from retry_notes): a PASS on a
        # retry or a refused probe says so, and why.
        if (retried or {}).get(item):
            shown += " (%s)" % "; ".join(retried[item])
        rows.append("| %s | **%s**%s · %s | %s |" % (mark, item, source, cell(wording), cell(shown)))
        sources += [source_line("**%s**" % item, anchor)] if anchor else []
    return rows, sources


def feature_rows(verdict_text, edits):
    """A feature re-walk (docs/feature-map.md) adds one row per feature; which features were required is
    the gate's to decide, so this lists what the explorer wrote and the gate's log names any missing.
    A re-walk is partial by design, so the states the explorer says it left unwalked are listed too, and so is
    what it found on the base branch's site for a state that failed (whether that counts is the gate's to say).
    The steps that judged a re-walk are the pull request's own copy, so a row says when it changed them (`edits`,
    {id: note} from feature_edits); a str is why that could not be read."""
    skipped, known = check_lines(verdict_text, "regression-skip"), check_lines(verdict_text, "regression-known")
    rows, sources = [], []
    for fid, carried in check_lines(verdict_text, "regression-check").items():
        row, found = feature_row(fid, carried, known.get(fid, []), skipped.get(fid, []),
                                 edits.get(fid) if isinstance(edits, dict) else None)
        rows.append(row)
        sources += found
    if rows:
        rows = ["", "**Features re-walked for regressions**", "", "| | Feature | What the browser showed | States not walked |",
                "|:-:|---|---|---|"] + rows
    if isinstance(edits, str):
        rows += ["", "Whether this pull request changed the Verify section of a re-walked feature could not be read: %s."
                 % edits.rstrip(".")]
    return rows, sources


def feature_row(fid, carried, known, skipped, edited):
    """One re-walked feature's row, and the steps its sentences were anchored to: its own, then the base branch's."""
    mark, shown, anchor = explored(carried, "regression-check")
    sources = [source_line("`%s`" % fid, anchor)] if anchor else []
    for line in known:
        base, base_anchor = split_anchor(said(line, "regression-known"))
        shown = then(shown, "The base branch fails the same way: %s" % base)
        sources += [source_line("`%s` on the base branch" % fid, base_anchor)] if base_anchor else []
    if edited:
        shown = then(shown, "Note: %s" % edited)
    unwalked = "; ".join(said(line, "regression-skip") for line in skipped)
    return "| %s | `%s` | %s | %s |" % (mark, fid, cell(shown), cell(unwalked)), sources


def explored(carried, token):
    """One verdict line split for the table: its mark, the explorer's sentence, and the step it anchored that sentence
    to. A criterion with no line, with more than one, or with a line that names neither PASS nor FAIL has no verdict."""
    if len(carried) != 1:
        return MARKS[None], "No verdict: the explorer wrote %s line for it." % ("no" if not carried else "more than one"), None
    shown, anchor = split_anchor(re.sub(r"^(?:PASS|FAIL)\b[ \t]*(?:%s)?[ \t]*" % DASH, "", said(carried[0], token), flags=re.I))
    shown = shown or "The explorer gave no sentence."
    if verdict(carried[0]) is None:
        return MARKS[None], "No verdict: the explorer's line names neither PASS nor FAIL. It says: %s" % shown, anchor
    return MARKS[verdict(carried[0])], shown, anchor


def then(text, more):
    """`more` as the next sentence after `text`, which the explorer may have ended without a full stop."""
    return "%s%s %s" % (text, "" if text.endswith((".", "!", "?")) else ".", more)


def split_anchor(text):
    """`<sentence> (qae/<file>::<step line>)` as (sentence, anchor); a sentence with no trailing anchor has none."""
    text = text.rstrip(". ")  # a full stop the explorer put after its anchor
    at = text.rfind("(qae/")
    if at < 0 or not text.endswith(")"):
        return text, None
    return text[:at].rstrip(), text[at + 1:-1]


def source_line(name, anchor):
    path, _, step = anchor.partition("::")
    return "- %s: `%s`%s" % (name, path, ", " + cell(step) if step else "")
