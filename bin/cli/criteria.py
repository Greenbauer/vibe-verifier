"""`criteria`: whether a pull request needs a browser check, its criteria's annotations and, asked, the explorers
they are split across; and the QAE manifest's qae-artifacts options `qae-inputs` reads too (the usage text in
bin/vibe-verifier says how)."""
import argparse
import json
import os
import subprocess
import sys

from _acceptance import (annotations, baseline_problems, body_criteria as read_criteria, carried, declares_none,
                         NO_CRITERIA, no_criteria_message, not_required, split_ticket, ticket_items)
import qae_artifacts
from _qae_rules import ENVIRONMENTAL
from cli.qae_shards import shard_plan
from cli.runner import GATES, fail, judged_entries, read_text


def criteria_command(args):
    if args.max_shards is not None and args.max_shards < 1:
        return fail("--max-shards is a whole number of explorers, 1 or more: %d" % args.max_shards)
    try:
        text = read_text(args.file)
        changed = [line for line in read_text(args.changed_files).splitlines() if line] if args.changed_files else None
        ticket_text = read_text(args.ticket) if args.ticket else ""
        carry = json.loads(read_text(args.carry)) if args.carry else None
    except (OSError, ValueError) as error:
        return fail("could not read %s" % error)
    coverage = criteria_ticket_coverage(args)
    if coverage:
        return coverage
    reason = not_required(text, changed, ticket_text, carry)
    if reason:
        return print_none(reason, carry, text)
    return print_criteria(args, text, ticket_text)


def print_none(reason, carry, text):
    """Print why no browser check is needed, and the earlier walk a pull request carries, when it carries one."""
    print("none: %s" % reason)
    walk = carried(carry, text) if read_criteria(text) and not declares_none(text) else None
    if walk:
        print("carried: %s %s" % (walk["head"], walk["url"]))
    return 0


def print_criteria(args, text, ticket_text):
    """Print the criteria a browser check walks, after refusing what no run can check."""
    ticket, problem = ticket_items(ticket_text, args.ticket)
    body = [] if declares_none(text) else read_criteria(text)
    walked, proven = split_ticket(ticket, text)
    unrunnable = unrunnable_problems(args, text, body, walked)
    if unrunnable:
        for message in unrunnable:
            print("::error::" + message, file=sys.stderr)
        return 1
    wanted = body + walked
    print("criteria: %d" % len(wanted))
    if problem:
        print("ticket: %s" % problem)
    for item, _ in proven:
        print("%s proven by a test, not walked" % item)
    for item, wording in wanted:
        notes = annotations(wording)
        for label, values in (("references", notes.references), ("roles", notes.roles), ("malformed", notes.malformed)):
            if values:
                print("%s %s: %s" % (item, label, ", ".join(values)))
    if args.max_shards is not None:
        print("\n".join(shard_plan(wanted, args.max_shards)))
    return 0


def unrunnable_problems(args, text, body, walked):
    """What no run can check fails here, before a harness spends a browser run on it: a body whose heading missed,
    a criterion judged against main or production, and one that names a role the preview has no login for."""
    unrunnable = baseline_problems(body)
    if not body and not declares_none(text) and no_criteria_message(text) != NO_CRITERIA:
        unrunnable.append(no_criteria_message(text))
    if args.roles is not None:
        unrunnable += role_problems(body + walked, args.roles)
    return unrunnable


def role_problems(items, roles):
    """One message per criterion naming, in `[as: ...]`, a role outside `roles` (a comma list, compared without
    case, as the gate reads step lines). The explorer fails such a criterion only after its walk."""
    logins = [role.strip() for role in roles.split(",") if role.strip()]
    known = {role.casefold() for role in logins}
    return ["%s names [as: %s], and this preview has a login only for %s: name one of those, or give the site "
            "step a login for it" % (item, role, ", ".join(logins) or "no role")
            for item, wording in items for role in annotations(wording).roles if role.casefold() not in known]


def criteria_ticket_coverage(args):
    """Run subscribed traceability before a browser skip; keep criteria's first-line protocol intact."""
    if not args.manifest:
        return 0
    for name, options in judged_entries(args, notice=sys.stderr):
        if name != "ticket-coverage":
            continue
        command = [sys.executable, os.path.join(GATES, "ticket_coverage.py"),
                   "--repo", args.repo, "--format", "github"]
        if args.base_ref:
            command += ["--base-ref", args.base_ref]
        result = subprocess.run(command + options, stdout=sys.stderr)
        if result.returncode:
            return result.returncode if result.returncode in (1, 2) else 2
    return 0


def manifest_artifacts_options(args):
    """The --widths, --allow-console and --allow-request on the manifest's qae-artifacts line, judged from the
    base, the way the gate reads them; None when there is no manifest or no such line."""
    if not args.manifest:
        return None
    lines = [gate_args for gate, gate_args in judged_entries(args, notice=sys.stderr) if gate == "qae-artifacts"]
    if not lines:
        return None
    parser = argparse.ArgumentParser(prog="qae-artifacts", add_help=False)
    parser.add_argument("--widths", type=qae_artifacts.width_list, default=None)
    for _, option, _ in ENVIRONMENTAL:
        parser.add_argument(option, action="append", default=[])
    try:
        return parser.parse_known_args(lines[0])[0]
    except SystemExit:
        sys.exit(fail("the qae-artifacts line in %s has a malformed --widths, --allow-console or --allow-request" % args.manifest))


def manifest_widths(args):
    """The --widths on the manifest's qae-artifacts line, judged from the base; [] when it has none."""
    options = manifest_artifacts_options(args)
    return (options.widths or []) if options else []


def manifest_environmental(args):
    """The `console: <regex>` and `request: <regex>` lines for what the manifest's qae-artifacts line declares
    environmental (its --allow-console and --allow-request), judged from the base; [] when it declares none."""
    options = manifest_artifacts_options(args)
    if not options:
        return []
    return ["%s: %s" % (kind, pattern) for kind, option, _ in ENVIRONMENTAL
            for pattern in getattr(options, option[2:].replace("-", "_"))]


def add_arguments(parser):
    parser.add_argument("file", help="the PR body or criteria document")
    parser.add_argument("--changed-files", metavar="FILE", help="the pull request's changed paths, one per line")
    parser.add_argument("--ticket", metavar="FILE", help="the criteria of the ticket the pull request implements, "
                        "counted as TC1, TC2, ... in addition to its own (empty: no ticket)")
    parser.add_argument("--max-shards", metavar="N", type=int, default=None, help="also print `shards: [1,...]`, how "
                        "many explorers the criteria are split across: enough for six walks each, at most N")
    parser.add_argument("--manifest", metavar="FILE", help="the QAE manifest: run subscribed ticket-coverage before "
                        "any browser skip")
    parser.add_argument("--carry", metavar="FILE", help="what actions/criteria/carry_facts.py read about the pull "
                        "request's last QAE walk: when its verdict still holds, prints `none:` naming it and `carried: <sha> <url>`")
    parser.add_argument("--roles", metavar="LIST", default=None, help="the roles the preview has a login for, comma "
                        "separated (the site step's): a criterion whose [as: ...] names another fails here")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--base-ref", default=None)
