"""What the proof audit grades, read the same way by its plan step (gates/_proof_audit.py) and its gate
(gates/proof_audit.py): the requirements a pull request's tickets define, and who takes the ones it splits off.

- Each `Ticket: <plan>#<anchor>` line of the body links a section of a plan in the repository: its `[req: ID]`
  items, read at the merge base exactly as ticket-coverage reads them, are requirements.
- Given a plan store (`--plans-endpoint`, its bearer token in $VV_PLANS_TOKEN), each `[plan:<id>]` marker in the
  title or body names a plan read from it when the step runs, and its whole wording, amendments included, is cut
  into requirements `PL-1`, `AM1-1`, ... (gates/_plan_store.py has the rules). They follow the Ticket sections'.
  A store that cannot be read, a marker with no token, or a plan with nothing to grade cannot run (exit 2). With
  no store given, a marker is not read, and a note says so.
- A body line `Split off: <ID>[, <ID> ...] -> <owner>`, the owner a pull request (`#123`, or its URL on github.com)
  or another plan (`[plan:<id>]`), gives those requirements of a stored plan to that owner: they are not graded,
  and the table lists each as SPLIT with its owner. A Ticket section's item goes to another section with
  `[takes: ID]` instead. A line that is not of that form, an ID no plan this pull request names in the store
  defines, an ID split off twice, or the plan this pull request implements as the owner cannot run: a split-off
  line never passes a requirement the plan does not define.

The lines are read outside code fences and HTML comments, as the markers are, and only by these two steps: the
model is never given the body.
"""
import re

from _acceptance import HTML_COMMENT
from _contract import CannotRun, Finding, git, matches, resolve_base
from _plan_store import PLAN_MARKER, SPLIT_LINE, plan_ids, plan_requirements, stored_plans
from _ticket import Unreadable, links, unfenced
import ticket_coverage

IDS = r"%s(?:[ \t]*,[ \t]*%s)*" % (ticket_coverage.ID, ticket_coverage.ID)
OWNER = r"#[0-9]+|https://github\.com/[\w.-]+/[\w.-]+/pull/[0-9]+/?|" + PLAN_MARKER.pattern
SPLIT = re.compile(r"^[ \t]*(?i:split[ \t]+off):[ \t]*(?P<ids>%s)[ \t]*->[ \t]*(?P<owner>%s)[ \t]*$" % (IDS, OWNER))
# A body's declared reason for linking no plan section, as `- None: <why>` declares no criteria.
NO_TICKET = re.compile(r"^\s*No ticket:\s*\w", re.I)  # a word, so a template's `No ticket: <why>` is no reason


def merge_base(repo, base_ref):
    return git(repo, "merge-base", resolve_base(repo, base_ref), "HEAD").strip()


def stored(item):
    """Whether a requirement comes from a plan store: its plan is `plan:<id>`, which no `Ticket:` link's path
    can be (a link's path holds no colon), so nothing reads it as a file of the repository."""
    return item["plan"].startswith("plan:")


def where(item):
    """The (path, line) a finding on the requirement annotates: none for a stored plan's, which is no file here."""
    return () if stored(item) else (item["plan"], item["line"])


def ticket_requirements(repo, base_ref, found):
    """The `[req: ID]` items of the linked sections at the merge base, in plan order, and a note for each section
    with no IDs. An unreadable file or anchor is CannotRun."""
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


def split_offs(body):
    """{ID: owner} of the body's `Split off:` lines, in the order written."""
    owners = {}
    for _, line in unfenced(HTML_COMMENT.sub("", body).splitlines()):
        if not SPLIT_LINE.match(line):
            continue
        split = SPLIT.match(line)
        if not split:
            raise CannotRun("`%s` is not a split-off line: write `Split off: <ID>[, <ID> ...] -> <#123, the pull "
                            "request's URL, or [plan:<id>]>`" % line.strip())
        # A pull request's URL is kept as `<owner>/<name>#123`, which GitHub links in the comment that allows no bare
        # URL (docs/comments.md).
        owner = re.sub(r"^https://github\.com/([\w.-]+/[\w.-]+)/pull/([0-9]+)/?$", r"\1#\2", split.group("owner"))
        for rid in re.split(r"[ \t]*,[ \t]*", split.group("ids")):
            if rid in owners:
                raise CannotRun("%s is split off twice" % rid)
            owners[rid] = owner
    return owners


def unknown(rid, items, plans):
    """The refusal of a `Split off:` line naming `rid`, which no stored plan's requirement is."""
    if any(item["id"] == rid for item in items):
        why = " (a Ticket section's item goes to another section that carries [takes: %s])" % rid
    elif not plans:
        why = " (no plan was read: none is named with [plan:<id>], or no store is given with --plans-endpoint)"
    else:
        why = ""
    return CannotRun("`Split off: %s`: no plan this pull request names in the plan store defines %s%s" % (rid, rid, why))


def split(items, plans, body):
    """Mark each requirement a `Split off:` line gives away with its owner (`split`), refusing any other line."""
    keyed, own = {item["id"]: item for item in items if stored(item)}, {plan_id for plan_id, _ in plans}
    for rid, owner in split_offs(body).items():
        if rid not in keyed:
            raise unknown(rid, items, plans)
        marker = PLAN_MARKER.fullmatch(owner)
        if marker and marker.group("id").lower() in own:
            raise CannotRun("`Split off: %s` names plan %s, which this pull request implements" % (rid, marker.group("id")))
        keyed[rid]["split"] = owner


def requirements(repo, base_ref, body, title="", endpoint="", token=""):
    """(requirements, notes): every requirement the body's Ticket links and the plans it names in the store define,
    each {id, text, plan, line, section, heading} and `split`, its owner, when a `Split off:` line gives it away;
    notes say why there is nothing to audit (no ticket, a linked section with no IDs, a marker with no store)."""
    try:
        found = links(body)
        plans = stored_plans(title, body, endpoint, token)
        items, notes = ticket_requirements(repo, base_ref, found) if found else ([], [])
        defined = {item["id"]: item["section"] for item in items}
        for item in plan_requirements(plans):
            if item["id"] in defined:
                raise CannotRun("%s is defined by %s and by %s" % (item["id"], defined[item["id"]], item["plan"]))
            items.append(item)
    except Unreadable as error:
        raise CannotRun(str(error))
    split(items, plans, body)
    if not found and not plans:
        notes.append("no ticket linked")
    if not endpoint and plan_ids(title, body):
        notes.append("the pull request names plan %s and no plan store is given (--plans-endpoint): it is not "
                     "audited" % plan_ids(title, body)[0])
    return items, notes


def unlinked(args, body):
    """The finding for a body that links no Ticket section yet must: one that claims `[req: ID]` items anyway
    (the section that defines them is unlinked, so none would be audited), or, with --require-ticket, one whose
    pull request changes a matching file and gives no `No ticket: <why>` line."""
    if links(body):
        return []
    lines = [line for _, line in unfenced(HTML_COMMENT.sub("", body).splitlines())]  # a template's example claims nothing
    claimed = list(dict.fromkeys(rid for line in lines for rid in ticket_coverage.ids(line)))
    if claimed:
        return [Finding("the body claims %d requirement ID(s) (%s) but no `Ticket: <plan>#<anchor>` line links the "
                        "section that defines them, so none of them is audited: add that line"
                        % (len(claimed), ", ".join(claimed)))]
    if not args.require_ticket or any(NO_TICKET.match(line) for line in lines):
        return []
    base = merge_base(args.repo, args.base_ref)
    changed = [path for path in git(args.repo, "diff", "--name-only", "--no-renames", "-z", base, "HEAD").split("\0")
               if path and matches(path, args.require_ticket)]
    if not changed:
        return []
    return [Finding("the pull request changes %d file(s) under --require-ticket (%s) and links no plan section: add "
                    "`Ticket: <plan>#<anchor>` for the section it implements, or a line `No ticket: <why>` saying why "
                    "none applies" % (len(changed), ", ".join(changed)))]
