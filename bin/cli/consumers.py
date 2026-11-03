"""`consumers`: the inventory and drift check of subscribed repositories, read through `gh api` (the usage text in
bin/vibe-verifier says what each row judges). `apply-down` reads the same stubs and pins."""
import base64
import os
import re
import shlex
import subprocess

from _pin_bump import CannotRead, gh_json
from cli.runner import ROOT, available, fail
from cli.wrapper import commit_status, git_in, load_wrapper, origin_repo, owner_repos, ruleset_rows, wrapper_calls

STUB_PATH = ".github/workflows/vibe-verifier.yml"
STUB_TEMPLATE = os.path.join(ROOT, "consumer", "vibe-verifier.yml")
WORKFLOWS_DIR = ".github/workflows"
# Any catalog action pinned in a workflow: the stub's gates@, a harness copy's gates@ and criteria@.
PIN_LINE = re.compile(r"(vibe-verifier/actions/[A-Za-z0-9_-]+@)([^\s#]+)([^\n]*)")
# The value of a job's `runs-on:` line (a label or a flow list) is the consumer's choice of runner.
# It stops at any character YAML reads as a line break, so nothing after it hides on the same line.
RUNS_ON = re.compile("^([ \t]*runs-on:)[^\n\r\x85  ]*", re.MULTILINE)
# The stub's and the review harness's name lines before docs/check-names.md, and each one's name now.
RENAMED = {"name: Vibe Verifier": "name: Standards", "    name: vibe-verifier-ok": "    name: Gates",
           "    name: review": '    name: "Code review: model run"', "    name: review-verify": "    name: Code review"}
OLD_NAME = re.compile("^(?:%s)$" % "|".join(map(re.escape, RENAMED)), re.MULTILINE)
RELEASE_PATHS = ["gates", "bin", "actions", "consumer", "tools", "rules"]  # a commit under these makes every older pin stale
REVIEW_HARNESS = os.path.join(ROOT, "harnesses", "review", "review.yml")
# The token line of a review harness copy; a wrapper names its own secret.
TOKEN_SECRET = re.compile(r"(claude_code_oauth_token: \$\{\{ secrets\.)[A-Za-z0-9_]+( \}\})")
# The one input a gates step takes its gate list from; a wrapper passes the list itself (`entries:`),
# because the repositories it runs in carry no manifest.
GATE_LIST_INPUT = re.compile(r"(vibe-verifier/actions/gates@[^\n]*\n\s+with:\n\s+)(?:manifest|entries): [^\n]*")
# What a repository subscribed through a wrapper must not carry: nothing reads it there.
MANIFESTS = (".vibe-verifier", ".vibe-verifier-review")
SOURCE = "source"  # this repository's own row: it carries no stub and its manifest is its own
WRAPPER = "wrapper"  # the wrapper's own row: it pins catalog actions for an organization and carries no stub
RULESET = "ruleset"  # a repository an organization ruleset subscribes through the wrapper: no stub expected
SETTLED = ("current", "ok", SOURCE, WRAPPER, RULESET)


def gh_file(repo, path):
    """The decoded file at `path` on the repository's default branch, or None when absent."""
    result = subprocess.run(["gh", "api", "repos/%s/contents/%s" % (repo, path), "--jq", ".content"],
                            capture_output=True, text=True)
    if result.returncode == 0:
        return base64.b64decode(result.stdout).decode("utf-8", errors="replace")
    if "HTTP 404" in result.stderr:
        return None
    raise CannotRead((result.stderr.strip() or "gh api failed").splitlines()[-1])


def workflow_texts(repo):
    """{path: text} for every workflow file under .github/workflows/, the stub included."""
    try:
        entries = gh_json("repos/%s/contents/%s" % (repo, WORKFLOWS_DIR))
    except CannotRead as error:
        if "HTTP 404" in str(error):
            return {}
        raise
    return {entry["path"]: gh_file(repo, entry["path"]) or "" for entry in entries
            if entry.get("type") == "file" and entry.get("path", "").endswith((".yml", ".yaml"))}


def workflow_pins(texts, with_stub=False):
    """{workflow path: ([pinned SHAs], text)} for every workflow that pins a catalog action. A harness
    copy (qae-explore.yml) pins gates@ and criteria@. The stub is left out unless asked for: a
    consumer's stub is judged on its own line of the row, and a wrapper's gate workflow is not a stub."""
    pins = {}
    for path, text in texts.items():
        found = [match.group(2) for match in PIN_LINE.finditer(text)]
        if found and (with_stub or path != STUB_PATH):
            pins[path] = (found, text)
    return pins


def jobs_of(text):
    """The workflow from its `jobs:` key on."""
    at = text.find("\njobs:\n")
    return text[at:] if at >= 0 else text


def comparable(text):
    """A copy of a catalog workflow with what its consumer owns blanked: each catalog pin and each
    `runs-on:` value. The `runs-on:` lines themselves still count, so a copy with more or fewer of
    them differs; `apply-down` rewrites only pin lines, so a bump keeps every runner. A name line
    docs/check-names.md renamed reads as its new name, so a copy made before it is not drift."""
    text = OLD_NAME.sub(lambda match: RENAMED[match.group(0)], text)
    return RUNS_ON.sub(r"\1 RUNNER", PIN_LINE.sub(r"\1SHA", text))


def harness_drift(path, text, wrapper=False):
    """What a harness copy changed outside its pin lines and `runs-on:` values, or None. Only review.yml
    is compared: every other line of it is the catalog's, so a copy that differs is running a review
    nobody released (qae-explore.yml has a consumer-owned build block and is not compared). A
    wrapper's copy owns its header (an organization ruleset requires it, and GitHub forbids a ruleset
    workflow cancel-in-progress, so it has no concurrency group), the name of its token secret, and
    the one line its verify step takes its gate list from (its repositories carry no
    .vibe-verifier-review, so it passes `entries:`): only its jobs are compared, with the secret named
    alike and that line left out."""
    if os.path.basename(path) != "review.yml":
        return None
    with open(REVIEW_HARNESS, encoding="utf-8") as handle:
        template = handle.read()
    if wrapper:
        text, template = (GATE_LIST_INPUT.sub(r"\1GATE-LIST", TOKEN_SECRET.sub(r"\1TOKEN\2", jobs_of(body)))
                          for body in (text, template))
    if comparable(text) == comparable(template):
        return None
    return "drifts from harnesses/review/review.yml %s" % (
        "under jobs: outside the pin, token and gate-list lines" if wrapper else "outside the pin line")


def source_git(args, *git_args):
    return git_in(args.source, *git_args)


def source_repo(args):
    """OWNER/NAME of the source checkout's origin, or None. Discovery skips it: the source
    repository dogfoods through its own ci.yml and never carries the consumer stub."""
    return origin_repo(args.source)


def pin_status(args, sha):
    """A catalog pin, judged against the source history: stale when a release commit came since."""
    return commit_status(args.source, args.source_ref, sha, RELEASE_PATHS, "release commit")


def stub_status(stub):
    """ok when the stub equals the template outside the pin line and `runs-on:` values; otherwise what differs."""
    if stub is None:
        return "missing", None
    pins = PIN_LINE.findall(stub)
    if len(pins) != 1:
        return "drift (%d pin lines, expected 1)" % len(pins), None
    with open(STUB_TEMPLATE, encoding="utf-8") as handle:
        template = handle.read()
    if comparable(stub) != comparable(template):
        return "drift (differs from consumer/vibe-verifier.yml outside the pin line)", pins[0][1]
    return "ok", pins[0][1]


def manifest_status(manifest):
    if manifest is None:
        return "missing", []
    gates = [shlex.split(line, comments=True) for line in manifest.splitlines()]
    gates = [words[0] for words in gates if words]
    if not gates:
        return "empty", []
    unknown = sorted(set(gates) - set(available()))
    return ("unknown gate %s" % ", ".join(unknown)) if unknown else "ok", gates


def wrapped_status(wrapper, repo, manifest):
    """(state, gates) for a repository the wrapper subscribes, the wrapper included: its gate list is
    the wrapper's entry for it. A manifest left in the repository (`manifest` is its .vibe-verifier)
    is read by nothing, so it is reported as stray rather than trusted."""
    path, text = wrapper.entry(repo)
    state, gates = manifest_status(text)
    problems = [] if state == "ok" else ["no %s in %s" % (path, wrapper.repo) if text is None else state]
    stray = [name for name in MANIFESTS if (manifest if name == ".vibe-verifier" else gh_file(repo, name)) is not None]
    if stray:
        problems.append("stray %s (the entry in %s is what runs)" % (" ".join(stray), wrapper.repo))
    return "; ".join(problems) or "ok", gates


def worst_status(args, shas):
    """One status for a file that may pin the same commit twice or two different ones."""
    statuses = [pin_status(args, sha) for sha in sorted(set(shas))]
    return "current" if all(s == "current" for s in statuses) else next(s for s in statuses if s != "current")


def catalog_rows(args, texts, wrapper=False):
    """[(workflow, status)] for every workflow pinning a catalog action, a review copy's drift included."""
    return [(os.path.basename(path), " ".join(filter(None, (worst_status(args, shas), harness_drift(path, text, wrapper)))))
            for path, (shas, text) in sorted(workflow_pins(texts, with_stub=wrapper).items())]


def native_status(repo):
    """(text, [problems]) for the enforcement GitHub itself holds: `sha_pinning_required`, the
    actions allowlist, and Dependabot alerts. Reading them needs an admin token, which a workflow's
    GITHUB_TOKEN is not, so drift back to `all` shows up here or nowhere.

    The pattern list is printed for review, never judged: GitHub holds the only copy of it and this
    tool carries no second one to compare against, so a widened list is visible in the row and does
    not flag. Same-owner actions are allowed by rule and need no pattern."""
    permissions = gh_json("repos/%s/actions/permissions" % repo)
    allowed = permissions.get("allowed_actions") or "unset"
    parts, problems = [], []
    if permissions.get("sha_pinning_required") is True:
        parts.append("sha-pin on")
    else:
        parts.append("sha-pin OFF")
        problems.append("sha_pinning_required is off")
    if allowed == "selected":
        patterns = gh_json("repos/%s/actions/permissions/selected-actions" % repo).get("patterns_allowed") or []
        parts.append("actions selected [%s]" % (" ".join(patterns) or "github-owned and same-owner only"))
    else:
        parts.append("actions %s" % allowed)
        problems.append("allowed_actions is %s, not selected" % allowed)
    if gh_ok("repos/%s/vulnerability-alerts" % repo):
        parts.append("alerts on")
    else:
        parts.append("alerts OFF")
        problems.append("Dependabot alerts are off")
    return ", ".join(parts), problems


def repo_row(args, repo, source, wrapper, targeted):
    """One inventory row, or None for a discovered repository that subscribes to nothing."""
    if repo == source:
        # The source repository subscribes through its own ci.yml, so only its native state
        # is judged; it is listed so every repository the catalog governs is watched.
        return (repo, "", SOURCE, SOURCE, [], [], native_status(repo))
    manifest, stub = gh_file(repo, ".vibe-verifier"), gh_file(repo, STUB_PATH)
    subscribed = manifest is not None or stub is not None or repo in targeted
    texts = workflow_texts(repo) if subscribed or wrapper else {}
    calls = [("%s (%s)" % (os.path.basename(path), wrapper.repo), wrapper.status(ref, called))
             for path, called, ref in wrapper_calls(wrapper, texts)] if wrapper else []
    if wrapper and repo == wrapper.repo:
        # The wrapper pins catalog actions in its own workflows, the gate workflow included, and
        # carries no stub. Its own pull requests run its gate workflow, so it needs an entry too.
        manifest_state, gates = wrapped_status(wrapper, repo, manifest)
        return (repo, "", WRAPPER, manifest_state, gates, catalog_rows(args, texts, wrapper=True), native_status(repo))
    if not subscribed:
        return unsubscribed_row(args, repo, calls)
    return subscriber_row(args, repo, (manifest, stub, texts, calls), wrapper, targeted)


def unsubscribed_row(args, repo, calls):
    """The row of a repository that subscribes to nothing itself, or None when discovery found it."""
    if calls:  # a caller of a wrapper workflow that subscribes to nothing itself: its pins are the row
        return (repo, "", "", "", [], calls, ("", []))
    if args.owner and repo not in (args.repo or []):
        return None  # discovery: an owner's repo that never subscribed is not a finding
    return (repo, "not subscribed", "missing", "missing", [], [], ("", []))


def subscriber_row(args, repo, read, wrapper, targeted):
    """The row of a subscribed repository: one a ruleset subscribes, or one that carries the stub."""
    manifest, stub, texts, calls = read
    if stub is None and repo in targeted:
        manifest_state, gates = wrapped_status(wrapper, repo, manifest)
        return (repo, "", RULESET, manifest_state, gates, catalog_rows(args, texts) + calls, native_status(repo))
    stub_state, sha = stub_status(stub)
    manifest_state, gates = manifest_status(manifest)
    return (repo, pin_status(args, sha) if sha else "", stub_state, manifest_state, gates,
            catalog_rows(args, texts) + calls, native_status(repo))


def consumers(args):
    source = source_repo(args)
    wrapper, error = load_wrapper(args)
    if error:
        return fail(error)
    try:
        discovered = set(owner_repos(args.owner)) if args.owner else set()
        rulesets, targeted = ruleset_rows(args, wrapper, discovered) if wrapper else ([], {})
    except CannotRead as error:
        return fail(str(error))
    repos = sorted(set(args.repo or []) | discovered | set(targeted) | ({wrapper.repo} if wrapper else set()))
    if not repos:
        return fail("no repositories given (use --repo OWNER/NAME or --owner OWNER)")
    if source_git(args, "rev-parse", "--verify", "--quiet", args.source_ref).returncode != 0:
        return fail("source ref %s not found in %s" % (args.source_ref, args.source))
    rows, unreadable = read_rows(args, repos, source, wrapper, targeted)
    needs_action = sum(print_row(args, row) for row in rows)
    needs_action += sum(print_ruleset(args, ruleset) for ruleset in rulesets)
    print(summary_line(rows, rulesets, needs_action, unreadable))
    return 2 if unreadable else (1 if needs_action else 0)


def read_rows(args, repos, source, wrapper, targeted):
    """(the rows of `repos`, how many could not be read)."""
    rows, unreadable = [], 0
    for repo in repos:
        try:
            row = repo_row(args, repo, source, wrapper, targeted)
        except CannotRead as error:
            row = (repo, "could not read: %s" % error, "", "", [], [], ("", []))
            unreadable += 1
        if row:
            rows.append(row)
    return rows, unreadable


def print_row(args, row):
    """Print one repository's row: True when it needs action."""
    repo, pin, stub_state, manifest_state, gates, workflows, (native, native_problems) = row
    problems = [text for text in (pin, stub_state, manifest_state) if text and text not in SETTLED]
    problems += ["%s %s" % (name, status) for name, status in workflows if status != "current"]
    problems += native_problems
    needs_action = report(args, repo, problems)
    print("%-32s pin: %-44s stub: %-12s manifest: %s%s%s%s" % (
        repo, pin or "-", stub_state or "-", manifest_state or "-",
        "  [%s]" % " ".join(gates) if gates else "",
        "  workflows: %s" % ", ".join("%s %s" % pair for pair in workflows) if workflows else "",
        "  native: %s" % native if native else ""))
    return needs_action


def print_ruleset(args, ruleset):
    """Print one ruleset's row, where an organization pins the wrapper: one row, however many repositories
    it targets. True when it needs action."""
    name, enforcement, targets, pins = ruleset
    problems = ["%s %s" % pair for pair in pins if pair[1] != "current"]
    problems += [] if enforcement == "active" else ["enforcement is %s, not active" % enforcement]
    problems += [] if targets is not None else ["targets repositories by something other than their names"]
    needs_action = report(args, "ruleset " + name, problems)
    print("%-32s enforcement: %s  targets: %s  pins: %s" % (
        "ruleset " + name, enforcement,
        "[%s]" % " ".join(repo.split("/", 1)[1] for repo in targets) if targets is not None else "not by name",
        ", ".join("%s %s" % pair for pair in pins)))
    return needs_action


def summary_line(rows, rulesets, needs_action, unreadable):
    rulesets_named = " and %d ruleset%s" % (len(rulesets), "" if len(rulesets) == 1 else "s") if rulesets else ""
    return "\n%d repositor%s%s: %d current, %d need action, %d could not be read" % (
        len(rows), "y" if len(rows) == 1 else "ies", rulesets_named,
        len(rows) + len(rulesets) - needs_action, needs_action - unreadable, unreadable)


def report(args, name, problems):
    """Annotate each problem in GitHub format when asked; True when there is any."""
    if args.format == "github":
        for problem in problems:
            print("::error title=vibe-verifier consumers::%s: %s" % (name, problem))
    return bool(problems)


def gh_ok(endpoint):
    """True for an endpoint that answers 204, False for one that answers 404. Anything else is
    CannotRead: a switch that cannot be read must not report as off."""
    result = subprocess.run(["gh", "api", endpoint], capture_output=True, text=True)
    if result.returncode == 0:
        return True
    last = (result.stderr.strip() or "gh api failed").splitlines()[-1]
    if "HTTP 404" in last:
        return False
    raise CannotRead(last)


def add_arguments(parser):
    parser.add_argument("--repo", action="append", metavar="OWNER/NAME", help="a repository to check (repeatable)")
    parser.add_argument("--owner", metavar="OWNER", help="check every non-archived repository of this owner that subscribes")
    parser.add_argument("--source", default=ROOT, help="a checkout of this repository, for pin ancestry (default: this one)")
    parser.add_argument("--source-ref", default="origin/main", help="the ref pins are judged against (default: origin/main)")
    parser.add_argument("--format", choices=["text", "github"], default="text")
    parser.add_argument("--wrapper", metavar="PATH", help="a checkout of a wrapper repository (an organization's CI "
                        "repository whose workflows its rulesets require): inventory it, its rulesets and its callers")
    parser.add_argument("--wrapper-ref", default="origin/main", help="the ref wrapper pins are judged against (default: origin/main)")
