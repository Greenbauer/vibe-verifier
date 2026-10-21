"""Merge grants, `bin/vibe-verifier merge-grant`: may an agent merge this pull request without a human review?

Not a gate, and not merge-readiness: it judges only what the pull request changes (and, for a grant that
asks, its repository and title), never its checks, reviews or mergeability. A merge guard and a
dashboard read the same answer from here, so the rule a merge is held to and the badge a person sees
cannot drift apart.

What counts is a policy, a JSON file each team writes for itself (docs/features/merge-grant.md).
Without one, _merge_grant.default.json decides: a pull request whose every changed path is CI,
Markdown, build or tool config, dependencies or tests, with none of them a migration, SQL, CODEOWNERS,
a security-named file or a workflow security line. A policy:

    {"version": 1, "extends": "default",
     "categories": {<name>: [<entry>, ...]},       a path is in a category when an entry's `path` matches
     "exclusions": {<name>: <exclusion>},          a path or line that keeps the merge a person's
     "grants": [<grant>, ...]}                     who may merge what; the first that holds decides

    entry:     {"path": RE, "ignore_case": bool, "json_changes_only": [key, ...], "json_unchanged": [key, ...]}
               a json_* condition is judged on every path its entry matches, whatever other category
               takes it, and must hold between the base and the head (`a.b` names a nested key; an
               absent file reads as {})
    exclusion: {"description": TEXT, "path": RE, "ignore_case": bool, "except": RE}, a hit when a changed
               path matches `path` and not `except` (case-sensitive whatever ignore_case says); with "lines": {"changed": RE, "removed": RE, "strip": RE, "ignore_case": bool}
               the path only selects files, and the hit is a changed (added or removed) or removed line
    grant:     {"name": TEXT, "repos": ["owner/name" or "owner/*", ...], "title": RE,
                "categories": [name, ...] or "any", "exclusions": [name, ...]}
               `repos` and `title` scope it (absent: every repository, any title); `categories` (default
               "any") lists where every changed path must fall

"extends": "default" starts from the default policy: categories and exclusions are added or replaced by
name, and `grants`, when given, replaces the default's list. Regular expressions are Python's, searched.

Fails closed: an unreadable file list, a list shorter than the pull request's count of changed files,
a file that cannot be read, or gh not running is a no, with the reason. Exit 0 prints `eligible:
<grant>`, exit 1 `not eligible: <reason>`, exit 2 `cannot run: <reason>` (a malformed policy).
"""
import base64
import difflib
import fnmatch
import json
import os
import re
import subprocess  # nosec B404: runs gh, the caller's own GitHub CLI, with fixed arguments
from urllib.parse import quote

DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_merge_grant.default.json")
ENTRY_KEYS = {"path", "ignore_case", "json_changes_only", "json_unchanged"}
EXCLUSION_KEYS = {"description", "path", "ignore_case", "except", "lines"}
LINES_KEYS = {"changed", "removed", "strip", "ignore_case"}
GRANT_KEYS = {"name", "repos", "title", "categories", "exclusions"}


class PolicyError(ValueError):
    """The policy file is malformed."""


class Unreadable(Exception):
    """GitHub did not give an answer the decision needs."""


def _only(value, keys, where):
    if not isinstance(value, dict):
        raise PolicyError("%s must be an object" % where)
    extra = set(value) - keys
    if extra:
        raise PolicyError("unknown %s field(s): %s" % (where, ", ".join(sorted(extra))))


def _regex(value, where, ignore_case=False):
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise PolicyError("%s must be a regular expression" % where)
    try:
        return re.compile(value, re.IGNORECASE if ignore_case else 0)
    except re.error as error:
        raise PolicyError("%s is not a valid regular expression: %s" % (where, error)) from None


def _keys(value, where):
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(key, str) and key for key in value):
        raise PolicyError("%s must be a list of key names" % where)
    return tuple(value)


def _entry(row, where):
    _only(row, ENTRY_KEYS, where)
    return {"path": _regex(row.get("path"), where + ".path", bool(row.get("ignore_case"))) or _missing(where + ".path"),
            "changes_only": _keys(row.get("json_changes_only"), where + ".json_changes_only"),
            "unchanged": _keys(row.get("json_unchanged"), where + ".json_unchanged")}


def _missing(where):
    raise PolicyError("%s is required" % where)


def _lines(value, where):
    if value is None:
        return None
    _only(value, LINES_KEYS, where)
    flag = bool(value.get("ignore_case"))
    return {"changed": _regex(value.get("changed"), where + ".changed", flag),
            "removed": _regex(value.get("removed"), where + ".removed", flag),
            "strip": _regex(value.get("strip"), where + ".strip", flag), "lower": flag}


def _exclusion(row, where):
    _only(row, EXCLUSION_KEYS, where)
    flag = bool(row.get("ignore_case"))
    description = row.get("description")
    if not isinstance(description, str) or not description:
        raise PolicyError("%s.description is required" % where)
    return {"description": description, "path": _regex(row.get("path"), where + ".path", flag) or _missing(where + ".path"),
            "except": _regex(row.get("except"), where + ".except"), "lines": _lines(row.get("lines"), where + ".lines")}


def _grant(row, where, categories, exclusions):
    _only(row, GRANT_KEYS, where)
    name, repos, wanted = row.get("name"), row.get("repos"), row.get("categories", "any")
    if not isinstance(name, str) or not name:
        raise PolicyError("%s.name is required" % where)
    if repos is not None and (not isinstance(repos, list) or not all(isinstance(repo, str) and "/" in repo for repo in repos)):
        raise PolicyError("%s.repos must be a list of owner/name patterns" % where)
    if wanted != "any" and (not isinstance(wanted, list) or not set(wanted) <= set(categories)):
        raise PolicyError('%s.categories must be "any" or names from categories' % where)
    named = row.get("exclusions", [])
    if not isinstance(named, list) or not set(named) <= set(exclusions):
        raise PolicyError("%s.exclusions must be names from exclusions" % where)
    return {"name": name, "repos": None if repos is None else tuple(repo.lower() for repo in repos),
            "title": _regex(row.get("title"), where + ".title"),
            "categories": None if wanted == "any" else tuple(wanted), "exclusions": tuple(named)}


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PolicyError("cannot read the policy %s: %s" % (path, error)) from None


def _merged(raw):
    _only(raw, {"version", "extends", "categories", "exclusions", "grants"}, "policy")
    if raw.get("version") != 1:
        raise PolicyError("policy version must be 1")
    if raw.get("extends") not in (None, "default"):
        raise PolicyError('policy extends must be "default"')
    base = _read_json(DEFAULT) if raw.get("extends") else {"categories": {}, "exclusions": {}, "grants": []}
    merged = {}
    for key in ("categories", "exclusions"):
        if not isinstance(raw.get(key, {}), dict):
            raise PolicyError("policy %s must be an object" % key)
        merged[key] = {**base[key], **raw.get(key, {})}
    merged["grants"] = raw.get("grants", base["grants"])
    if not isinstance(merged["grants"], list) or not merged["grants"]:
        raise PolicyError("policy grants must be a non-empty list")
    return merged


def load_policy(path=None):
    """The policy at `path` (the default policy when None), validated and compiled."""
    merged = _merged(_read_json(path or DEFAULT))
    categories = {}
    for name, rows in merged["categories"].items():
        if not isinstance(rows, list) or not rows:
            raise PolicyError("category %s must be a non-empty list of entries" % name)
        categories[name] = [_entry(row, "category %s" % name) for row in rows]
    exclusions = {name: _exclusion(row, "exclusion %s" % name) for name, row in merged["exclusions"].items()}
    grants = [_grant(row, "grant %d" % index, categories, exclusions) for index, row in enumerate(merged["grants"], 1)]
    return {"categories": categories, "exclusions": exclusions, "grants": grants}


def _in_scope(grant, repo, title):
    if grant["repos"] is not None and not any(fnmatch.fnmatchcase(repo.lower(), pattern) for pattern in grant["repos"]):
        return False
    return grant["title"] is None or bool(grant["title"].search(title()))


def _json(text, path):
    if text is None:
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = None
    if not isinstance(value, dict):
        raise _No("%s is not a JSON object" % path)
    return value


def _without(value, keys):
    """`value` with each (dotted) key removed, and a parent the removal left empty removed too."""
    value = json.loads(json.dumps(value))
    for key in keys:
        parts = key.split(".")
        parent = value
        for part in parts[:-1]:
            parent = parent.get(part) if isinstance(parent, dict) else None
        if isinstance(parent, dict):
            parent.pop(parts[-1], None)
            if len(parts) > 1 and value.get(parts[0]) == {}:
                value.pop(parts[0])
    return value


def _at(value, key):
    """The value at a (dotted) key, or None where any part is missing."""
    for part in key.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def _holds(entry, row, read):
    """None when the entry's content condition holds for this file, else why not."""
    if not (entry["changes_only"] or entry["unchanged"]):
        return None
    _, old, new = row
    base, head = _json(read(old, "base"), old), _json(read(new, "head"), new)
    if entry["changes_only"] and _without(base, entry["changes_only"]) != _without(head, entry["changes_only"]):
        return "%s changes more than %s" % (new, ", ".join(entry["changes_only"]))
    for key in entry["unchanged"] or ():
        if _at(base, key) != _at(head, key):
            return "%s changes its %s" % (new, key)
    return None


class _No(Exception):
    """A reason the grant does not hold."""


def _paths(rows):
    return sorted({path for _, old, new in rows for path in (old, new) if path})


def _categorized(grant, policy, rows, read):
    entries = [entry for name in grant["categories"] for entry in policy["categories"][name]]
    outside = [path for path in _paths(rows) if not any(entry["path"].search(path) for entry in entries)]
    if outside:
        raise _No("it changes files outside %s (%s)" % (", ".join(grant["categories"]), " ".join(outside[:3])))
    # A conditional entry is judged whenever it matches, by either of the file's paths, even when another
    # category also takes the path: e2e/package.json is a test path, and its scripts still run on install.
    for row in rows:
        why = next(filter(None, (_holds(entry, row, read) for entry in entries
                                 if entry["path"].search(row[1]) or entry["path"].search(row[2]))), None)
        if why:
            raise _No(why)

def _selected(exclusion, path):
    return bool(path) and bool(exclusion["path"].search(path)) and not (exclusion["except"] and exclusion["except"].search(path))


def _in_context(lines):
    """Each line keyed by the less-indented lines above it (its YAML keys), so a block moved under another
    job reads as removed and added, where a bare line diff would keep it as unchanged."""
    stack, keyed = [], []
    for line in lines:
        text = line.strip()
        if text and not text.startswith("#"):
            indent = len(line) - len(line.lstrip())
            while stack and stack[-1][0] >= indent:
                stack.pop()
            stack.append((indent, text))
        keyed.append("\x1f".join(text for _, text in stack[:-1]) + "\x00" + line)
    return keyed


def _changed_lines(lines, base, head):
    """The removed and added lines, each diffed in its context as written, then folded and stripped."""
    # split("\n"), not splitlines(): a form feed or other Unicode break must not cut a line in two.
    a, b = (base or "").split("\n"), (head or "").split("\n")
    removed, added = [], []
    matcher = difflib.SequenceMatcher(None, _in_context(a), _in_context(b), autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            removed += a[i1:i2]
            added += b[j1:j2]

    def prepare(line):
        line = line.lower() if lines["lower"] else line
        return lines["strip"].sub(" ", line) if lines["strip"] else line
    return [prepare(line) for line in removed], [prepare(line) for line in added]


def _line_hit(lines, base, head):
    removed, added = _changed_lines(lines, base, head)
    if lines["changed"] and any(lines["changed"].search(line) for line in removed + added):
        return True
    return bool(lines["removed"]) and any(lines["removed"].search(line) for line in removed)


def _path_hits(exclusion, rows):
    hits = [path for path in _paths(rows) if _selected(exclusion, path)]
    if hits:
        raise _No("it touches %s (%s)" % (exclusion["description"], " ".join(hits[:3])))


def _line_hits(exclusion, rows, read):
    for _, old, new in rows:
        if (_selected(exclusion, old) or _selected(exclusion, new)) and _line_hit(
                exclusion["lines"], read(old, "base"), read(new, "head")):
            raise _No("%s changes %s" % (new, exclusion["description"]))


def _excluded(grant, policy, rows, read):
    for name in grant["exclusions"]:
        exclusion = policy["exclusions"][name]
        if exclusion["lines"] is None:
            _path_hits(exclusion, rows)
        else:
            _line_hits(exclusion, rows, read)

def _judge(grant, policy, files, read):
    if grant["categories"] is None and not grant["exclusions"]:
        return None
    rows = files()
    if grant["categories"] is not None:
        _categorized(grant, policy, rows, read)
    _excluded(grant, policy, rows, read)
    return None


def decide(policy, repo, *, files, title, read):
    """(grant name, None) when a grant holds, else (None, why not).

    files() -> [(status, path at the base, path at the head)], every file the pull request changes;
    title() -> its title; read(path, "base" | "head") -> the file's text, or None when it is absent there.
    Each is called only when a grant in scope needs it, and raises Unreadable when GitHub does not answer."""
    reasons, cache = [], {}

    def cached_read(path, side):
        if (path, side) not in cache:
            cache[(path, side)] = read(path, side)
        return cache[(path, side)]
    for grant in policy["grants"]:
        try:
            if not _in_scope(grant, repo, title):
                continue
            _judge(grant, policy, files, cached_read)
            return grant["name"], None
        except _No as why:
            reasons.append("%s: %s" % (grant["name"], why))
        except Unreadable as why:
            reasons.append("%s: %s" % (grant["name"], why))
    return None, "; ".join(reasons) or "no grant covers %s" % repo


# ---- reading a pull request from GitHub with gh, for the command ----

def _gh(gh, *args):
    try:
        result = subprocess.run([gh, *args], capture_output=True, text=True, timeout=60)  # nosec B603
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Unreadable("gh did not run: %s" % error) from None
    return result


def _gh_ok(gh, what, *args):
    result = _gh(gh, *args)
    if result.returncode != 0:
        raise Unreadable("%s could not be read from GitHub" % what)
    return result.stdout


class PullRequest:
    """One pull request read through gh, each answer read once and only when asked for. The files come from
    the files API (paginated, as the dashboard reads them), never `gh pr diff`, which GitHub refuses for a
    large diff."""

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
                                 "--json", "title", "--jq", ".title").strip()
        return self._title

    def files(self):
        count = self.view()["changedFiles"]
        listing = _gh_ok(self.gh, "its changed files", "api", "repos/%s/pulls/%s/files" % (self.repo, self.number),
                         "--paginate", "--jq", '.[] | "\\(.status)\\t\\(.previous_filename // .filename)\\t\\(.filename)"')
        rows = [tuple(parts) for parts in (line.split("\t") for line in listing.splitlines()) if len(parts) == 3]
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


def contents_text(value):
    """The text of a contents-API answer (base64 JSON), for a caller with its own reader."""
    if not isinstance(value, dict) or value.get("encoding") != "base64" or not isinstance(value.get("content"), str):
        raise Unreadable("GitHub did not give the file's content")
    try:
        return base64.b64decode(value["content"]).decode("utf-8")
    except (ValueError, UnicodeError):
        raise Unreadable("the file is not UTF-8 text") from None


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
