#!/usr/bin/env python3
"""Fail when a pull request adds a finding of the repository's declared ast-grep rules.

A repository writes its established patterns down as ast-grep rules (YAML), each with a `message`
that tells whoever wrote the code what to do instead. The rules come from:

    --rules DIR   a rule directory in the repository (repeatable; default: .vibe-verifier-rules,
                  when the base or the head has it)
    --pack NAME   a catalog pack, rules/NAME/ in this catalog at its pinned revision (repeatable)

A rule directory holds rule files (*.yml, *.yaml) and, under tests/, their ast-grep rule tests.

A ratchet: a finding counts only when its fingerprint (rule id, path, and the matched text with its
whitespace collapsed; no line numbers) occurs more often at HEAD than the same rules find at the
merge base, so what the pull request did not add never blocks, and a file it moved is compared with
itself at its old path. --all reports every finding in every tracked file instead, with no base.

Base-controlled, like the manifest: a rule file the base has is judged as the base has it, even when
the pull request edits or deletes it; a rule file the pull request adds applies at once. Packs come
from the catalog itself. Every rule needs a non-empty message and a rule test with at least one
valid and one invalid case, and every rule test must pass; otherwise the gate cannot run (exit 2)
and names the rule.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter

from _contract import CannotRun, Finding, added_files, changed_files, git, renamed, resolve_base, run_gate, tracked_files
from _tools import ensure

GATE = "repo-rules"
PACKS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "rules")
DEFAULT_RULES = ".vibe-verifier-rules"
CHUNK = 500  # paths per ast-grep call, far below any platform's argument limit
# The standard library reads no YAML, so ast-grep's own YAML grammar reads the rule files: each
# document, each top-level key of a document, and each item of a top-level sequence.
_TOP = ("utils: {top: {kind: block_mapping_pair, "
        "inside: {kind: block_mapping, inside: {kind: block_node, inside: {kind: document}}}}}")
READER = "\n---\n".join([
    "id: doc\nlanguage: yaml\nrule: {kind: document}",
    "id: key\nlanguage: yaml\n%s\nrule: {matches: top, has: {field: key, pattern: $KEY}}" % _TOP,
    "id: item\nlanguage: yaml\n%s\nrule:\n  any:\n"
    "    - {kind: block_sequence_item, inside: {kind: block_sequence, inside: {kind: block_node, inside: {matches: top}}}}\n"
    "    - {kind: flow_node, inside: {kind: flow_sequence, inside: {kind: flow_node, inside: {matches: top}}}}" % _TOP,
])


def is_yaml(path):
    return path.endswith((".yml", ".yaml"))


def show(repo, rev, path):
    shown = subprocess.run(["git", "-C", repo, "show", "%s:%s" % (rev, path)], capture_output=True)
    return shown.stdout if shown.returncode == 0 else None


def read(repo, path):
    with open(os.path.join(repo, path), "rb") as handle:
        return handle.read()


def judged_files(args, base, directory, tracked):
    """{path under the directory: bytes} as this run judges them, and the paths judged as at the base
    although the head differs. With a base: every file the base has, as the base has it (one the pull
    request moved at its new path), and the files the pull request added. Without one: the head's."""
    prefix = directory + "/"
    if base is None:
        return {p[len(prefix):]: read(args.repo, p) for p in tracked if p.startswith(prefix) and is_yaml(p)}, []
    at_base = [p for p in git(args.repo, "ls-tree", "-r", "-z", "--name-only", base, "--", directory).split("\0") if is_yaml(p)]
    moved = {old: new for new, old in renamed(args.repo, base).items() if new.startswith(prefix)}
    files, differs = {}, []
    for path in at_base:
        here = moved.get(path, path)
        files[here[len(prefix):]] = text = show(args.repo, base, path)
        if here not in tracked or read(args.repo, here) != text:
            differs.append(here)
    for path in added_files(args.repo, base):
        if path.startswith(prefix) and is_yaml(path) and path[len(prefix):] not in files:
            files[path[len(prefix):]] = read(args.repo, path)
    return files, differs


def directory_files(root):
    """{path under root: bytes} for a directory read as it is on disk: a pack, or one outside the repository."""
    files = {}
    for here, _, names in os.walk(root):
        for name in filter(is_yaml, names):
            files[os.path.relpath(os.path.join(here, name), root).replace(os.sep, "/")] = read(here, name)
    return files


def pack_files(name):
    root = os.path.join(PACKS, name)
    if not re.match(r"^[a-z0-9][a-z0-9-]*$", name) or not os.path.isdir(root):
        packs = sorted(n for n in os.listdir(PACKS) if os.path.isdir(os.path.join(PACKS, n)))
        raise CannotRun("no catalog pack %r (packs: %s)" % (name, ", ".join(packs) or "none"))
    return directory_files(root)


def rule_sources(args, base, tracked):
    """[(where, {path: bytes})] for every pack and rule directory this run judges by.

    A relative --rules is a directory of the repository, judged from the base. An absolute one lies
    outside it, such as one a wrapper writes, and is read as it is: a pull request cannot edit it."""
    sources = [("catalog:rules/%s/" % name, pack_files(name)) for name in args.pack or []]
    explicit = [os.path.normpath(d).replace(os.sep, "/") for d in args.rules or []]
    for directory in explicit or [DEFAULT_RULES]:
        if os.path.isabs(directory):
            files, differs = directory_files(directory), []
        else:
            files, differs = judged_files(args, base, directory, set(tracked))
        for path in differs:
            print("%s: %s is judged as it is at %s; this branch's change to it applies once it merges" % (GATE, path, base),
                  file=sys.stderr)
        if files:
            sources.append((directory + "/", files))
        elif explicit:
            raise CannotRun("--rules %s holds no rule files" % directory)
    for where, files in sources:
        if not any(not name.startswith("tests/") for name in files):
            raise CannotRun("%s holds no rules, only tests" % where)
    if not sources:
        raise CannotRun("no rules to run: add %s/, or pass --rules DIR or --pack NAME" % DEFAULT_RULES)
    return sources


def assemble(sources, project):
    """Write every source into one ast-grep project; return {path in the project: where it came from}."""
    origin = {}
    for number, (where, files) in enumerate(sources):
        for name, data in files.items():
            kind, inner = ("tests", name[len("tests/"):]) if name.startswith("tests/") else ("rules", name)
            assembled = "%s/%d/%s" % (kind, number, inner)
            os.makedirs(os.path.dirname(os.path.join(project, assembled)), exist_ok=True)
            with open(os.path.join(project, assembled), "wb") as handle:
                handle.write(data)
            origin[assembled] = where + name
    for kind in ("rules", "tests"):
        os.makedirs(os.path.join(project, kind), exist_ok=True)
    with open(os.path.join(project, "sgconfig.yml"), "w", encoding="utf-8") as handle:
        handle.write("ruleDirs: [rules]\ntestConfigs:\n  - testDir: tests\n")
    return origin


def relabel(text, project, origin):
    """ast-grep's own output, with the project's temporary paths put back as the files they came from."""
    for root in {project, os.path.realpath(project)}:  # a temporary directory may be reached through a symlink
        text = text.replace(root + os.sep, "")
    for assembled, where in origin.items():
        text = text.replace(assembled, where)
    return text.strip()


def scalar(text):
    """A YAML scalar as a rule's id or message writes it: plain, quoted, or a block."""
    text = text.strip()
    if text[:1] in ("|", ">"):
        return text.partition("\n")[2].strip()
    if len(text) > 1 and text[0] == text[-1] == "'":
        return text[1:-1].replace("''", "'")
    if len(text) > 1 and text[0] == text[-1] == '"':
        try:
            return json.loads(text)
        except ValueError:
            return text[1:-1]
    return "" if text in ("~", "null", "Null", "NULL") else text


def documents(sg, project):
    """{path in the project: [{top-level key: (value, items in its sequence)}]}, one dict per YAML document."""
    result = subprocess.run([sg, "scan", "--inline-rules", READER, "--json=stream", "--", "rules", "tests"],
                            cwd=project, capture_output=True, text=True)
    if result.returncode != 0:
        raise CannotRun("ast-grep could not read the rule files: %s" % (result.stderr.strip() or "no output"))
    hits = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]

    def spans(rule):
        return [(h["file"], h["range"]["byteOffset"]["start"], h["range"]["byteOffset"]["end"], h) for h in hits if h["ruleId"] == rule]
    keys = {}
    for path, start, end, hit in spans("key"):
        label = hit["metaVariables"]["single"]["KEY"]["text"]
        keys[(path, start, end)] = [scalar(label), hit["text"][len(label):].lstrip()[1:], 0]  # value: past the key's colon
    for path, offset, _, _ in spans("item"):
        for (where, start, end), entry in keys.items():
            if where == path and start <= offset < end:
                entry[2] += 1
    docs = {}
    for path, start, end, _ in sorted(spans("doc"), key=lambda span: span[:3]):
        docs.setdefault(path, []).append({key: (value, items) for (where, s, _), (key, value, items) in keys.items()
                                          if where == path and start <= s < end})
    return docs


def verify(sg, project, origin):
    """Refuse a rule that cannot tell anyone what to do instead (no message) or that nothing proves
    (no passing rule test with both a valid and an invalid case)."""
    tested = subprocess.run([sg, "test", "--config", "sgconfig.yml", "--skip-snapshot-tests", "--include-off", "--color", "never"],
                            cwd=project, capture_output=True, text=True)
    if tested.returncode != 0:
        raise CannotRun("the rule tests do not pass:\n%s" % relabel(tested.stdout + tested.stderr, project, origin))
    rules, cases = {}, {}
    for path, docs in documents(sg, project).items():
        for keys in docs:
            if "id" not in keys:
                continue
            rule_id = scalar(keys["id"][0])
            if path.startswith("rules/"):
                rules[rule_id] = (origin[path], scalar(keys.get("message", ("", 0))[0]))
            else:
                count = cases.setdefault(rule_id, [0, 0])
                count[0] += keys.get("valid", ("", 0))[1]
                count[1] += keys.get("invalid", ("", 0))[1]
    problems = []
    for rule_id, (where, message) in sorted(rules.items()):
        valid, invalid = cases.get(rule_id, (0, 0))
        if not message:
            problems.append("rule %s (%s) has no message; say what to write instead" % (rule_id, where))
        if not valid or not invalid:
            problems.append("rule %s (%s) has %d valid and %d invalid test cases under tests/; it needs at least one of each"
                            % (rule_id, where, valid, invalid))
    if problems:
        raise CannotRun("\n".join(problems))


def scan(sg, project, root, paths):
    """Every finding of the assembled rules in paths (relative to root), in a stable order."""
    hits = []
    for start in range(0, len(paths), CHUNK):
        result = subprocess.run([sg, "scan", "--config", os.path.join(project, "sgconfig.yml"), "--off=unused-suppression",
                                 "--json=stream", "--", *paths[start:start + CHUNK]], cwd=root, capture_output=True, text=True)
        found = [json.loads(line) for line in result.stdout.splitlines() if line.strip()] if result.returncode in (0, 1) else []
        if result.returncode not in (0, 1) or (result.returncode == 1 and not found):
            raise CannotRun("ast-grep scan exited %d: %s" % (result.returncode, (result.stderr.strip() or "no output").splitlines()[-1]))
        hits += found
    return sorted(hits, key=lambda h: (h["file"], h["range"]["start"]["line"], h["range"]["start"]["column"], h["ruleId"]))


def fingerprint(hit):
    return hit["ruleId"], hit["file"], " ".join(hit["text"].split())


def finding(hit, context=""):
    text = "%s: %s%s" % (hit["ruleId"], hit["message"], context)
    if hit.get("note"):
        text += "\n  note: " + hit["note"].strip().replace("\n", "\n  ")
    return Finding(text, hit["file"], hit["range"]["start"]["line"] + 1)


def check(args):
    base = None if args.all else resolve_base(args.repo, args.base_ref)
    tracked = tracked_files(args.repo)
    sources = rule_sources(args, base, tracked)
    sg = ensure("ast-grep")
    with tempfile.TemporaryDirectory(prefix="vv-rules-") as work:
        project = os.path.join(work, "project")
        verify(sg, project, assemble(sources, project))
        if args.all:
            return [finding(hit) for hit in scan(sg, project, args.repo, sorted(tracked))]
        listed = set(tracked)
        head = scan(sg, project, args.repo, sorted(p for p in changed_files(args.repo, base) if p in listed))
        merge_base = git(args.repo, "merge-base", base, "HEAD").strip()
        moved = renamed(args.repo, base)
        snapshot, at_base = os.path.join(work, "base"), []
        for path in sorted({hit["file"] for hit in head}):
            text = show(args.repo, merge_base, moved.get(path, path))  # a moved file is compared with itself at its old path
            if text is None:
                continue  # new in this pull request
            os.makedirs(os.path.dirname(os.path.join(snapshot, path)), exist_ok=True)
            with open(os.path.join(snapshot, path), "wb") as handle:
                handle.write(text)
            at_base.append(path)
        before = Counter(fingerprint(hit) for hit in scan(sg, project, snapshot, at_base))
    now = Counter(fingerprint(hit) for hit in head)
    return [finding(hit, " (new in this pull request)" if not before[fingerprint(hit)] else
                    " (%d in this file now, %d at the merge base)" % (now[fingerprint(hit)], before[fingerprint(hit)]))
            for hit in head if now[fingerprint(hit)] > before[fingerprint(hit)]]


def add_arguments(parser):
    parser.add_argument("--rules", action="append", metavar="DIR", help="a rule directory in the repository (repeatable; "
                        "default %s when present)" % DEFAULT_RULES)
    parser.add_argument("--pack", action="append", metavar="NAME", help="a catalog pack, rules/NAME/ (repeatable)")
    parser.add_argument("--all", action="store_true", help="report every finding in every tracked file, with no ratchet")


if __name__ == "__main__":
    run_gate(GATE, __doc__, check, add_arguments)
