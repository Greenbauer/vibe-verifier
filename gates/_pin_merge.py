"""What `apply-down --auto --act --merge` does with the pin bumps already open, before it plans new ones.

A bump is this tool's own pull request (a branch of the repository named like _pin_bump.BRANCH_PREFIXES), so once
it is merge-ready it is squash-merged here, held to what a person would check: it is into the default branch;
every changed file is a workflow whose changed lines are `uses: owner/name/path@<sha>` pins of the catalog (or, for
a wrapper bump, of a workflow of the repository's own owner) that keep their target and move only the commit, so
no CODEOWNERS, permission or secret line can ride along (narrower than the default merge grant,
gates/_merge_grant.default.json); each moved pin is the commit the branch is named for, and that commit is on the
catalog's main (the wrapper's, for a wrapper bump), so a branch someone else pushed cannot pin an older or a
fork's commit, whose own run would make the gate job green; the gate job
passed on the head and every other check there concluded without failing; no review asks for changes; the head is
not behind its base; and GitHub says it merges cleanly. Never with admin rights, and only at the head it judged.
What is not ready yet is left, or moved on:

- behind its base: the base is merged in (a merge commit, never a rebase); it merges on a later run, once its
  checks pass on the new head;
- conflicting with its base, or with nothing left to change (its pins reached the base another way): closed with
  its branch, so the plan opens a fresh one from the base while that commit is still wanted;
- checks still running, or GitHub still computing whether it can merge (or reporting it `unstable` from before
  they finished): left for a later run;
- a failed check, no gate job, a review asking for changes, a line that is not a pin, or a block GitHub names:
  held for a person, and the run exits 1.

It runs before the plan, so a bump opened in this run is judged on a later one, after its checks have registered.
"""
import base64
import functools
import re
from urllib.parse import quote

import _pin_bump
from _pin_bump import BRANCH_PREFIXES, CannotRead

#: The gate job of a consumer's stub, by its current and its older name: the proof the new catalog passes there.
GATE_CHECKS = ("Gates", "vibe-verifier-ok")
PASSED = ("success", "skipped", "neutral")
PIN = re.compile(r"^(\s*(?:-\s+)?uses:\s*)([\w.-]+/[\w.-]+/[^@\s]+)@([0-9a-f]{40})(\s+#.*)?$")
WORKFLOW = re.compile(r"\.github/workflows/[^/]+\.ya?ml")


def merge_pass(repos, on_main):
    """Judge every open bump of `repos`, printing one line each. `on_main(prefix)` is a test of whether a commit SHA
    is on the main the bump branch `prefix` pins from (_pin_bump.older_than). Returns 2 when a read or write failed,
    1 when a bump is held for a person, else 0."""
    code = 0
    for repo in repos:
        try:
            pulls = [pull["number"] for pull in _pin_bump.open_pulls(repo) if is_bump(repo, pull)]
        except CannotRead as error:
            print("%-32s could not list its bumps: %s" % (repo, error))
            code = 2
            continue
        for number in pulls:
            try:
                held, what = judge(repo, number, on_main)
            except CannotRead as error:
                held, what = 2, "failed: %s" % error
            print("%-32s #%d %s" % (repo, number, what))
            code = max(code, held)
    return code


def pinned_from(source, target, wrapper):
    """merge_pass's `on_main`: for a bump branch prefix, a test of whether a commit is on the main that level pins
    from, the catalog checkout `source` up to `target`, or the wrapper's head (no commit, without a wrapper)."""
    def test(prefix):
        if prefix != BRANCH_PREFIXES[1]:
            return _pin_bump.older_than(source, target)
        return _pin_bump.older_than(wrapper.checkout, wrapper.head()[0]) if wrapper else (lambda short: False)
    return test


def is_bump(repo, pull):
    head = pull.get("head") or {}
    return head.get("ref", "").startswith(BRANCH_PREFIXES) and (head.get("repo") or {}).get("full_name") == repo


def judge(repo, number, on_main):
    """(exit code, what was done) for one open bump."""
    pull = _pin_bump.gh_json("repos/%s/pulls/%d" % (repo, number))
    head, branch, base = pull["head"]["sha"], pull["head"]["ref"], pull["base"]["ref"]
    if base != _pin_bump.gh_json("repos/%s" % repo).get("default_branch"):
        return 1, "held: it is into %s, not the default branch" % base
    if pull.get("changed_files") == 0:
        return close(repo, number, "Nothing left to change: %s already has these pins." % base)
    compare = _pin_bump.gh_json("repos/%s/compare/%s...%s" % (repo, quote(base, safe=""), head))
    why = not_pins(repo, compare, head, branch, on_main)
    if why:
        return 1, "held: %s" % why
    if pull.get("mergeable") is None:
        return 0, "waiting: GitHub is still working out whether it merges"
    if not pull["mergeable"]:
        return close(repo, number, "Conflicts with %s; the next pin bump opens a fresh one from %s." % (base, base))
    if compare.get("behind_by"):
        _pin_bump.gh_json("repos/%s/pulls/%d/update-branch" % (repo, number), method="PUT", body={"expected_head_sha": head})
        return 0, "%d commit(s) behind %s: merged %s in; it merges once its checks pass there" % (compare["behind_by"], base, base)
    waiting = checks(repo, head, branch.startswith(BRANCH_PREFIXES[0])) or reviews(repo, number)
    if waiting:
        return waiting
    state = pull.get("mergeable_state")
    if state in ("unstable", "unknown"):  # read before the last check finished, or not yet worked out
        return 0, "waiting: GitHub says it is %s" % state
    if state not in ("clean", "has_hooks"):
        return 1, "held: GitHub says it is %s" % state
    _pin_bump.gh_json("repos/%s/pulls/%d/merge" % (repo, number), method="PUT", body={
        "merge_method": "squash", "sha": head, "commit_title": "%s (#%d)" % (pull["title"], number)})
    try:
        _pin_bump.gh_json("repos/%s/git/refs/heads/%s" % (repo, branch), method="DELETE")
    except CannotRead:
        pass  # the repository deletes merged branches itself, or a later bump resets this one
    return 0, "squash-merged at %s" % head[:10]


def close(repo, number, why):
    _pin_bump.close(repo, number, why)
    return 0, "closed: %s" % why


def not_pins(repo, compare, head, branch, on_main):
    """Why the bump's changes are not pin lines alone, each to the commit the branch is named for, or None."""
    files = compare.get("files") or []
    if not files or len(files) >= 300:  # the compare API lists at most 300 files
        return "GitHub listed %d changed files" % len(files)
    prefix = next(p for p in BRANCH_PREFIXES if branch.startswith(p))
    named, pinned_from = branch[len(prefix):], on_main(prefix)
    if not pinned_from(named):
        return "%s is not a commit on the main it pins from" % named
    target = (r"[\w.-]+/vibe-verifier/actions/" if prefix == BRANCH_PREFIXES[0]
              else r"%s/[\w.-]+/\.github/workflows/" % re.escape(repo.split("/")[0]))
    pin = functools.partial(moved_pin, target=target, named=named, pinned_from=pinned_from)
    refs = (compare["merge_base_commit"]["sha"], head)
    return next(filter(None, (file_not_pins(repo, changed, refs, pin, named) for changed in files)), None)


def file_not_pins(repo, changed, refs, pin, named):
    """Why one changed file of not_pins is not a workflow whose changed lines all `pin`, or None."""
    path = changed.get("filename", "")
    if changed.get("status") != "modified" or not WORKFLOW.fullmatch(path):
        return "%s is %s, not a workflow whose pins moved" % (path, changed.get("status"))
    old, new = (text(repo, path, ref) for ref in refs)
    if old is None or new is None:
        return "%s could not be read" % path
    old, new = old.split("\n"), new.split("\n")
    if len(old) != len(new):
        return "%s adds or removes lines" % path
    other = next((after for before, after in zip(old, new) if before != after and not pin(before, after)), None)
    return None if other is None else "%s changes a line that is not a pin to %s: %s" % (path, named, other.strip()[:80])


def moved_pin(before, after, target, named, pinned_from):
    """Whether two lines are the same `uses:` of a `target` action or workflow, the second at the commit `named`,
    whose whole SHA `pinned_from` finds on its main: ten digits are not enough to tell a fork's commit apart."""
    old, new = PIN.match(before), PIN.match(after)
    return bool(old and new and old.group(1, 2) == new.group(1, 2) and re.match(target, new.group(2))
                and new.group(3).startswith(named) and pinned_from(new.group(3)))


def text(repo, path, ref):
    """The file at `ref`, or None when the contents API gives no text (a file past 1 MB)."""
    answer = _pin_bump.gh_json("repos/%s/contents/%s?ref=%s" % (repo, quote(path), ref))
    return base64.b64decode(answer["content"]).decode() if answer.get("encoding") == "base64" else None


def checks(repo, head, gated):
    """(exit code, why) while a check on `head` is running, failed, or the gate job (when `gated`) has not passed;
    None when every check passed."""
    results = check_results(repo, head)
    failed = sorted({name for name, result in results if result and result not in PASSED})
    if failed:
        return 1, "held: %s failed on its head" % ", ".join(failed)
    running = [name for name, result in results if result is None]
    if running:
        return 0, "waiting: %d check(s) still running" % len(running)
    if gated and not any(name in GATE_CHECKS and result == "success" for name, result in results):
        return 1, "held: no gate job (%s) passed on its head" % " or ".join(GATE_CHECKS)
    if not any(result == "success" for _, result in results):
        return 1, "held: no check passed on its head"
    return None


def check_results(repo, head):
    """[(name, conclusion or None while running)] of every check run and commit status on `head`."""
    runs = paged("repos/%s/commits/%s/check-runs" % (repo, head), "check_runs")
    statuses = paged("repos/%s/commits/%s/status" % (repo, head), "statuses")
    results = [(run.get("name"), run.get("conclusion") if run.get("status") == "completed" else None) for run in runs]
    return results + [(status.get("context"), None if status.get("state") == "pending" else status.get("state"))
                      for status in statuses]


def reviews(repo, number):
    latest = {}
    for review in paged("repos/%s/pulls/%d/reviews" % (repo, number), None):
        if review.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[(review.get("user") or {}).get("login")] = review["state"]
    asking = sorted(login for login, state in latest.items() if state == "CHANGES_REQUESTED")
    return (1, "held: %s asked for changes" % ", ".join(asking)) if asking else None


def paged(endpoint, key):
    """Every item of a listing, page by page (`key` names the list in an object answer)."""
    items, page = [], 1
    while True:
        answer = _pin_bump.gh_json("%s?per_page=100&page=%d" % (endpoint, page))
        batch = (answer.get(key) or []) if key else answer
        items += batch
        if len(batch) < 100:
            return items
        page += 1
