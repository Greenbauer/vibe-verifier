"""`bin/vibe-verifier merge-grant`: read a pull request through gh and print _merge_grant's decision.

The files come from the files API (paginated, as the dashboard reads them), never `gh pr diff`, which
GitHub refuses for a large diff. Exit 0 prints `eligible: <grant>`, exit 1 `not eligible: <reason>`,
exit 2 `cannot run: <reason>` (a malformed policy).
"""
import json
import subprocess  # nosec B404: runs gh, the caller's own GitHub CLI, with fixed arguments
from urllib.parse import quote

from _merge_grant import PolicyError, Unreadable, decide, load_policy


def _gh(gh, *args):
    try:
        result = subprocess.run([gh, *args], capture_output=True, timeout=60)  # nosec B603
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Unreadable("gh did not run: %s" % error) from None
    # Bytes, decoded leniently: a binary file (a logo under .github/) is still read, and judged as text.
    return subprocess.CompletedProcess(result.args, result.returncode, result.stdout.decode("utf-8", "replace"),
                                       result.stderr.decode("utf-8", "replace"))


def _gh_ok(gh, what, *args):
    result = _gh(gh, *args)
    if result.returncode != 0:
        raise Unreadable("%s could not be read from GitHub" % what)
    return result.stdout


class PullRequest:
    """One pull request read through gh, each answer read once and only when asked for."""

    def __init__(self, gh, repo, number):
        self.gh, self.repo, self.number, self._view, self._title = gh, repo, str(number), None, None

    def view(self):
        if self._view is None:
            text = _gh_ok(self.gh, "it", "pr", "view", self.number, "--repo", self.repo,
                          "--json", "baseRefOid,headRefOid,changedFiles")
            try:
                view = json.loads(text)
            except json.JSONDecodeError:
                view = {}
            if not all(view.get(key) not in (None, "") for key in ("baseRefOid", "headRefOid", "changedFiles")):
                raise Unreadable("GitHub did not give its base, head and file count")
            self._view = view
        return self._view

    def title(self):
        if self._title is None:
            self._title = _gh_ok(self.gh, "its title", "pr", "view", self.number, "--repo", self.repo,
                                 "--json", "title", "--jq", ".title").rstrip("\n")
        return self._title

    def files(self):
        count = self.view()["changedFiles"]
        listing = _gh_ok(self.gh, "its changed files", "api", "repos/%s/pulls/%s/files" % (self.repo, self.number),
                         "--paginate", "--jq", '.[] | [.status, (.previous_filename // .filename), .filename] | @json')
        try:
            # One JSON array a line: a newline or tab inside a file name stays inside its string.
            rows = [tuple(json.loads(line)) for line in listing.splitlines() if line]
        except ValueError:
            raise Unreadable("GitHub's file listing could not be read") from None
        if not rows or len(rows) != count:
            raise Unreadable("GitHub listed %d of its %s changed files" % (len(rows), count))
        return rows

    def read(self, path, side):
        ref = self.view()["baseRefOid" if side == "base" else "headRefOid"]
        result = _gh(self.gh, "api", "repos/%s/contents/%s?ref=%s" % (self.repo, quote(path), ref),
                     "-H", "Accept: application/vnd.github.raw")
        if result.returncode == 0:
            return result.stdout
        if "HTTP 404" in result.stderr:
            return None
        raise Unreadable("%s could not be read at the %s" % (path, side))


def add_arguments(parser):
    parser.add_argument("--repo", required=True, metavar="OWNER/NAME")
    parser.add_argument("--pr", required=True, metavar="N")
    parser.add_argument("--policy", metavar="FILE", help="the merge policy (default: gates/_merge_grant.default.json)")
    parser.add_argument("--gh", default="gh", help="the gh to read GitHub with (default: gh on PATH)")


def command(args):
    try:
        policy = load_policy(args.policy)
    except PolicyError as error:
        print("cannot run: %s" % error)
        return 2
    pull = PullRequest(args.gh, args.repo, args.pr)
    grant, why = decide(policy, args.repo, files=pull.files, title=pull.title, read=pull.read)
    if grant:
        print("eligible: %s" % grant)
        return 0
    print("not eligible: %s" % why)
    return 1
