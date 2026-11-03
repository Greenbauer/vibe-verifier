"""A wrapper repository for `consumers` and `apply-down` (`--wrapper PATH`): an organization's CI repository that
subscribes its repositories to the catalog, its rulesets that require its workflows, and the git and gh reads both
subcommands judge a pin with."""
import fnmatch
import os
import re
import subprocess

from _pin_bump import CannotRead, gh_json


def owner_repos(owner):
    result = subprocess.run(["gh", "repo", "list", owner, "--limit", "200", "--no-archived", "--json", "nameWithOwner",
                             "--jq", ".[].nameWithOwner"], capture_output=True, text=True)
    if result.returncode != 0:
        raise CannotRead((result.stderr.strip() or "gh repo list failed").splitlines()[-1])
    return sorted(result.stdout.split())


def git_in(checkout, *git_args):
    return subprocess.run(["git", "-C", checkout, *git_args], capture_output=True, text=True)


def origin_repo(checkout):
    """OWNER/NAME of a checkout's origin remote, or None."""
    url = git_in(checkout, "remote", "get-url", "origin").stdout.strip()
    match = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/?$", url)
    return match.group(1) if match else None


def commit_status(checkout, ref, sha, paths, noun):
    """current | stale (N <noun>s behind) | unknown | unpinned: a pin of `sha` judged against `ref`
    in `checkout`, stale when a commit since it touched `paths`."""
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        return "unpinned (%s is not a commit SHA)" % sha
    if git_in(checkout, "merge-base", "--is-ancestor", sha, ref).returncode != 0:
        return "unknown (not in %s)" % ref
    behind = git_in(checkout, "rev-list", "--count", "%s..%s" % (sha, ref), "--", *paths).stdout.strip()
    return "current" if behind == "0" else "stale (%s %s%s behind)" % (behind, noun, "" if behind == "1" else "s")


# A wrapper's gate list for one repository of its organization, by name, in the wrapper's checkout.
ENTRY = "repos/%s/vibe-verifier"


class Wrapper:
    """A repository that subscribes an organization's repositories to the catalog on their behalf.
    Its own workflows pin catalog actions; the organization's rulesets require its workflows, pinned
    by commit, and a caller may still name one in a `uses:` line. `checkout` is a local clone judged
    at `ref`, the way --source is for the catalog."""

    def __init__(self, checkout, ref):
        self.checkout, self.ref = checkout, ref
        self.repo = origin_repo(checkout)
        self.owner = (self.repo or "/").split("/")[0]
        # A caller's pin of one of its workflows: OWNER/NAME/.github/workflows/<file>@<ref>
        self.call = re.compile(r"(?<![\w.-])(%s/(\.github/workflows/[^@\s]+)@)([^\s#]+)([^\n]*)"
                               % re.escape(self.repo or "-"), re.IGNORECASE)

    def status(self, sha, path):
        """A pin of one wrapper workflow: stale when a commit since it touched that workflow."""
        return commit_status(self.checkout, self.ref, sha, [path], "commit")

    def head(self):
        """(commit, date) of the ref every wrapper pin is judged against and bumped to."""
        sha = git_in(self.checkout, "rev-parse", "--verify", "--quiet", self.ref).stdout.strip()
        return sha, git_in(self.checkout, "log", "-1", "--date=short", "--format=%cd", sha).stdout.strip()

    def entry(self, repo):
        """(path, text or None): the gate list the wrapper carries for OWNER/NAME at its ref."""
        path = ENTRY % repo.split("/", 1)[1]
        shown = git_in(self.checkout, "show", "%s:%s" % (self.ref, path))
        return path, shown.stdout if shown.returncode == 0 else None


def load_wrapper(args):
    """(Wrapper, None), (None, why) when --wrapper is not a usable checkout, or (None, None) without it."""
    if not args.wrapper:
        return None, None
    wrapper = Wrapper(args.wrapper, args.wrapper_ref)
    if not wrapper.repo:
        return None, "--wrapper %s is not a checkout with an origin remote" % args.wrapper
    if not re.fullmatch(r"[0-9a-f]{40}", wrapper.head()[0]):
        return None, "ref %s not found in %s" % (args.wrapper_ref, args.wrapper)
    return wrapper, None


def wrapper_calls(wrapper, texts):
    """[(workflow path, called wrapper workflow path, ref)] for every `uses:` of a wrapper workflow."""
    return [(path, match.group(2), match.group(3)) for path, text in sorted(texts.items())
            for match in wrapper.call.finditer(text)]


def required_workflows(rules):
    """Every workflow entry of the `workflows` rules in `rules` (the live objects, so a bump edits them)."""
    return [entry for rule in rules if rule.get("type") == "workflows"
            for entry in (rule.get("parameters") or {}).get("workflows") or []]


def wrapper_rulesets(wrapper):
    """(the wrapper's repository id, [(ruleset, [(workflow path, pinned sha or ref)])]) for every ruleset
    of the wrapper's organization whose `workflows` rule requires a wrapper workflow. A user account
    has no organization rulesets (HTTP 404): an empty list, not an error."""
    wrapper_id = gh_json("repos/%s" % wrapper.repo).get("id")
    try:
        listed = gh_json("orgs/%s/rulesets?per_page=100" % wrapper.owner)
    except CannotRead as error:
        if "HTTP 404" in str(error):
            return wrapper_id, []
        raise
    found = []
    for summary in listed:
        ruleset = gh_json("orgs/%s/rulesets/%s" % (wrapper.owner, summary["id"]))
        pins = [(entry.get("path", ""), entry.get("sha") or entry.get("ref") or "")
                for entry in required_workflows(ruleset.get("rules") or []) if entry.get("repository_id") == wrapper_id]
        if pins:
            found.append((ruleset, pins))
    return wrapper_id, found


def ruleset_targets(ruleset, repos):
    """The OWNER/NAME repositories among `repos` a ruleset targets by name, or None when its
    conditions target another way (a repository id list, a custom property), which is not resolved."""
    conditions = ruleset.get("conditions") or {}
    by_name = conditions.get("repository_name")
    if by_name is None or set(conditions) - {"ref_name", "repository_name"}:
        return None

    def hit(name, patterns):
        return any(pattern == "~ALL" or fnmatch.fnmatchcase(name.lower(), pattern.lower()) for pattern in patterns)
    return sorted(repo for repo in repos if hit(repo.split("/", 1)[1], by_name.get("include") or [])
                  and not hit(repo.split("/", 1)[1], by_name.get("exclude") or []))


def ruleset_rows(args, wrapper, discovered):
    """([(ruleset name, enforcement, targets or None, [(workflow, status)])], {target: [ruleset names]})."""
    _, found = wrapper_rulesets(wrapper)
    if not found:
        return [], {}
    same_owner = args.owner and args.owner.lower() == wrapper.owner.lower()
    repos = discovered if same_owner else set(owner_repos(wrapper.owner))
    rows, targeted = [], {}
    for ruleset, pins in found:
        name = "%s/%s" % (wrapper.owner, ruleset.get("name"))
        targets = ruleset_targets(ruleset, repos)
        for repo in targets or []:
            targeted.setdefault(repo, []).append(name)
        rows.append((name, ruleset.get("enforcement") or "unset", targets,
                     [(os.path.basename(path), wrapper.status(sha, path)) for path, sha in pins]))
    return rows, targeted
