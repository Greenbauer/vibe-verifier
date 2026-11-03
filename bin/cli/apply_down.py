"""`apply-down`: bump stale pins, and nothing else. A plan and its digest first; `--confirm <digest>` writes one pull
request per repository and level, and each ruleset's pins (the usage text in bin/vibe-verifier says how; `--auto`
is gates/_pin_bump.py's)."""
import copy
import hashlib
import os
import re

import _pin_bump
from _pin_bump import CannotRead, gh_json, open_bump
from cli.consumers import PIN_LINE, RELEASE_PATHS, STUB_PATH, gh_file, pin_status, source_git, source_repo, stub_status, workflow_texts
from cli.runner import ROOT, fail
from cli.wrapper import load_wrapper, owner_repos, required_workflows, wrapper_calls, wrapper_rulesets


def bump_plan(args, target, wrapper=None):
    """([(repo, wrapper or None, new pin, [(path, [old pins], new text)])], [(repo, why skipped)]).

    Per repository, two levels: the catalog pins (a consumer's stub must match its template; a wrapper
    has no stub), moved to `target`; and the pins of the wrapper's workflows a caller names in a
    `uses:` line, moved to the wrapper's head (its repository names that level). Each level becomes
    its own pull request."""
    planned, skipped = [], []
    head = wrapper.head() if wrapper else (None, None)
    for repo in args._repos:
        bumps, reasons = repo_plan(args, target, wrapper, head, repo)
        planned += bumps
        skipped += reasons
    return planned, skipped


def repo_plan(args, target, wrapper, head, repo):
    """(planned bumps, skipped reasons) for one repository of bump_plan."""
    try:
        stub, (state, sha), texts = read_stub(repo, wrapper)
    except CannotRead as error:
        return [], [(repo, "could not read: %s" % error)]
    is_wrapper = wrapper is not None and repo == wrapper.repo
    calls = wrapper_calls(wrapper, texts) if wrapper and not is_wrapper else []
    pinned = state == "ok" or is_wrapper
    skipped = stub_skipped(args, repo, stub, state, pinned, calls)
    if skipped is None:
        return [], []  # discovery: a repository that never subscribed is not a finding
    catalog = catalog_bumps(args, target, texts) if pinned else []
    wrapped = wrapper_bumps(wrapper, head, calls, texts)
    planned = [(repo, None, target, catalog)] if catalog else []
    planned += [(repo, wrapper.repo, head[0], wrapped)] if wrapped else []
    if not planned and (pinned or calls):
        skipped.append((repo, "already pinned to %s" % target[:10] if sha == target else "pins are current"))
    return planned, skipped


def read_stub(repo, wrapper):
    """(the stub or None, its (state, pin), the workflows): the workflows only where a bump may read them."""
    stub = gh_file(repo, STUB_PATH)
    state, sha = stub_status(stub)
    return stub, (state, sha), workflow_texts(repo) if state == "ok" or wrapper else {}


def stub_skipped(args, repo, stub, state, pinned, calls):
    """[] or [(repo, "stub <state>")] for a stub a bump will not touch; None for a repository discovery found that
    neither subscribes nor calls the wrapper."""
    if pinned:
        return []
    if stub is not None or repo in (args.repo or []):
        return [(repo, "stub %s" % state)]
    return [] if calls else None


def catalog_bumps(args, target, texts):
    """[(path, [old pins], new text)] for each workflow pinning a catalog action anywhere but a current commit."""
    bumps = []
    for path, text in sorted(texts.items()):
        moving = sorted({m.group(2) for m in PIN_LINE.finditer(text)
                         if m.group(2) != target and not pin_status(args, m.group(2)).startswith("current")})
        if moving:
            bumps.append((path, moving, PIN_LINE.sub(
                lambda m: "%s%s # main %s" % (m.group(1), target, args._target_date) if m.group(2) in moving else m.group(0), text)))
    return bumps


def wrapper_bumps(wrapper, head, calls, texts):
    """[(path, [old pins], new text)] for each workflow calling a wrapper workflow anywhere but a current commit."""
    wrapper_head, wrapper_date = head
    bumps = []
    for path in sorted({path for path, _, _ in calls}):
        moving = sorted({ref for called_from, called, ref in calls if called_from == path and ref != wrapper_head
                         and not wrapper.status(ref, called).startswith("current")})
        if moving:
            bumps.append((path, moving, wrapper.call.sub(
                lambda m: "%s%s # main %s" % (m.group(1), wrapper_head, wrapper_date) if m.group(3) in moving else m.group(0), texts[path])))
    return bumps


def ruleset_plan(wrapper):
    """[(ruleset, [(workflow path, old pin)], new rules)] for every ruleset that pins a wrapper workflow
    anywhere but a current commit. Only those entries' `sha` changes, to the wrapper's head."""
    head, _ = wrapper.head()
    wrapper_id, found = wrapper_rulesets(wrapper)
    moves = []
    for ruleset, pins in found:
        moving = sorted({(path, old) for path, old in pins if old != head and wrapper.status(old, path) != "current"})
        if not moving:
            continue
        rules = copy.deepcopy(ruleset.get("rules") or [])
        for entry in required_workflows(rules):
            if entry.get("repository_id") == wrapper_id and (entry.get("path", ""), entry.get("sha") or entry.get("ref") or "") in moving:
                entry["sha"] = head
        moves.append((ruleset, moving, rules))
    return moves


def apply_down(args):
    wrapper, error = load_wrapper(args)
    if error:
        return fail(error)
    target, code = bump_target(args, wrapper)
    if code is not None:
        return code
    planned, moves, held, unread = plan_bumps(args, target, wrapper)
    if not planned and not moves:
        print("\nnothing to bump")
        return 2 if unread else held
    lines = [(repo, path, sha, to) for repo, _, to, changes in planned for path, shas, _ in changes for sha in shas]
    lines += [("ruleset %s/%s" % (wrapper.owner, ruleset.get("name")), path, old, wrapper.head()[0])
              for ruleset, moving, _ in moves for path, old in moving]
    digest = hashlib.sha256("\n".join("%s %s %s %s" % line for line in lines).encode()).hexdigest()[:12]
    for repo, path, sha, to in lines:
        print("%-32s %-40s %s -> %s" % (repo, path, sha[:10], to[:10]))
    code = confirmed(args, (planned, moves), target, digest, held)
    if code is not None:
        return code
    failed = open_bumps(args, wrapper, planned)
    failed = update_rulesets(wrapper, moves) or failed
    return 2 if failed else held


def bump_target(args, wrapper):
    """(the commit catalog pins move to, None) after setting args._repos and args._target_date, or (None, exit code)."""
    try:
        discovered = set(owner_repos(args.owner)) - {source_repo(args)} if args.owner else set()
    except CannotRead as error:
        return None, fail(str(error))
    args._repos = sorted(set(args.repo or []) | discovered | ({wrapper.repo} if wrapper else set()))
    if not args._repos:
        return None, fail("no repositories given (use --repo OWNER/NAME or --owner OWNER)")
    resolved = source_git(args, "rev-parse", "--verify", "--quiet", args.to or args.source_ref)
    if resolved.returncode != 0:
        return None, fail("ref %s not found in %s" % (args.to or args.source_ref, args.source))
    target = resolved.stdout.strip()
    # The comment beside the pin says when the pin was taken; a bump that leaves the old date there
    # makes the line lie about the commit next to it.
    args._target_date = source_git(args, "log", "-1", "--date=short", "--format=%cd", target).stdout.strip()
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", args._target_date):
        return None, fail("could not read the date of %s in %s" % (target[:10], args.source))
    return target, None


def plan_bumps(args, target, wrapper):
    """(planned pull requests, ruleset moves, the exit code for a held bump, whether a read failed), printing what
    is skipped and what a tool move holds."""
    planned, skipped = bump_plan(args, target, wrapper)
    tools = _pin_bump.holds(args.source, planned, target)
    if args.auto:  # a tool move waits for its sweep (GATE-CONTRACT.md); ruleset pins are the wrapper's to move
        planned = [bump for bump in planned if bump[1] is not None or bump[0] not in tools]
    moves = []
    if wrapper and not args.auto:
        try:
            moves = ruleset_plan(wrapper)
        except CannotRead as error:
            skipped.append(("rulesets of %s" % wrapper.owner, "could not read: %s" % error))
    for repo, reason in skipped:
        print("%-32s skipped: %s" % (repo, reason))
    for repo, reasons in sorted(tools.items()):
        print("".join("%-32s %s: %s\n" % (repo, "held" if args.auto else "note", why) for why in reasons), end="")
    held = int(bool(args.auto and tools))
    return planned, moves, held, any("could not read" in reason for _, reason in skipped)


def confirmed(args, plan, target, digest, held):
    """None when the plan is to be written now, else the exit code: `--auto` decides when itself, and without
    it only `--confirm` with the plan's own digest writes."""
    planned, moves = plan
    if args.auto:
        try:
            go, why = _pin_bump.when(args, planned, target, RELEASE_PATHS)
        except CannotRead as error:
            return fail("could not read the lane: %s" % error)
        print("\n%s: %s%s" % ("bump now" if go else "waiting", why, "" if args.act or not go else ". Dry run: nothing written"))
        if not (go and args.act):
            return held
        args.confirm = digest
    if not args.confirm:
        print("\n%d pull request(s) and %d ruleset update(s) to make. Re-run with --confirm %s" % (len(planned), len(moves), digest))
        return 1
    if args.confirm != digest:
        return fail("plan digest is %s, not %s: the repositories moved since the plan, so read it again" % (digest, args.confirm))
    return None


def open_bumps(args, wrapper, planned):
    """Open (or find open) each planned pull request and close the bumps it supersedes; True when one failed."""
    failed = False
    for repo, level, to, changes in planned:
        branch = "ci/%s-pin-%s" % ("wrapper" if level else "vibe-verifier", to[:10])
        try:
            default_branch = gh_json("repos/%s" % repo)["default_branch"]
            existing = gh_json("repos/%s/pulls?head=%s:%s&state=open" % (repo, repo.split("/")[0], branch))
            url = existing[0]["html_url"] if existing else open_bump(repo, changes, to, branch, default_branch, level)
            print("%-32s %s" % (repo, url))
            older = _pin_bump.older_than(wrapper.checkout if level else args.source, to)
            print("".join("%-32s closed %s, superseded\n" % (repo, old) for old in _pin_bump.supersede(repo, branch, url, older)), end="")
        except CannotRead as error:
            print("%-32s failed: %s" % (repo, error))
            failed = True
    return failed


def update_rulesets(wrapper, moves):
    """PUT each ruleset's moved pins; True when one failed."""
    failed = False
    for ruleset, moving, rules in moves:
        name = "ruleset %s/%s" % (wrapper.owner, ruleset.get("name"))
        try:
            gh_json("orgs/%s/rulesets/%s" % (wrapper.owner, ruleset["id"]), method="PUT", body={"rules": rules})
            print("%-32s updated: %s" % (name, ", ".join(os.path.basename(path) for path, _ in moving)))
        except CannotRead as error:
            print("%-32s could not update: %s" % (name, error))
            failed = True
    return failed


def add_arguments(parser):
    parser.add_argument("--repo", action="append", metavar="OWNER/NAME", help="a repository to bump (repeatable)")
    parser.add_argument("--owner", metavar="OWNER", help="bump every subscribed repository of this owner")
    parser.add_argument("--source", default=ROOT, help="a checkout of this repository (default: this one)")
    parser.add_argument("--source-ref", default="origin/main", help="the ref pins are judged against (default: origin/main)")
    parser.add_argument("--to", metavar="REF", default=None, help="pin to this ref instead of --source-ref")
    parser.add_argument("--confirm", metavar="DIGEST", default=None, help="the digest the plan printed; without it nothing is written")
    parser.add_argument("--wrapper", metavar="PATH", help="a checkout of a wrapper repository: bump its catalog pins, its "
                        "callers' pins of its workflows and its organization's ruleset pins")
    parser.add_argument("--wrapper-ref", default="origin/main", help="the wrapper ref pins move to (default: origin/main)")
    _pin_bump.add_arguments(parser)
