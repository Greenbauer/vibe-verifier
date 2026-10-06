#!/usr/bin/env python3
"""Fail when a pull request adds a finding of the repository's declared ast-grep rules.

A repository writes its established patterns down as ast-grep rules (YAML), each with a `message`
that tells whoever wrote the code what to do instead. The rules come from:

    --rules DIR   a rule directory in the repository (repeatable; default: .vibe-verifier-rules,
                  when the base or the head has it)
    --pack NAME   a catalog pack, rules/NAME/ in this catalog at its pinned revision (repeatable)

A rule directory holds rule files (*.yml, *.yaml) and, under tests/, their ast-grep rule tests. An
sgconfig.yml at its root is read for one key, languageGlobs (ast-grep's format), which maps more file
extensions to a language for that directory's rules and tests only: each directory runs as its own
ast-grep project, so one directory's mapping never changes what another's rules see.

A ratchet by count, the way ESLint's bulk suppressions work: a file violates a rule only when it has
more of that rule's findings at HEAD than at the merge base (a file the pull request moved is
compared with itself at its old path), so editing, restyling or moving a violation within its file
never blocks. Fixing one and adding one in the same file nets zero and passes. When a count rises,
the findings on lines the pull request added or changed are reported; when none is on such a line,
all of that rule's findings in the file are, with the count before and after. --all reports every
finding in every tracked file instead, with no base.

A rule blocks unless its file says `severity: warning`, `info` or `hint`: those findings are printed
as advisory and never fail the gate, --all included. An unset severity blocks, although ast-grep
itself reads it as hint, so a rule is advisory only when it says so.

Base-controlled, like the manifest: a rule file the base has is judged as the base has it, even when
the pull request edits or deletes it; a rule file the pull request adds applies at once, an
sgconfig.yml it adds once it merges (a mapping can narrow what rules see). Packs come
from the catalog itself. Every rule needs a non-empty message and a rule test with at least one
valid and one invalid case, and every rule test must pass; otherwise the gate cannot run (exit 2)
and names the rule.

    --doc PATH    a file of the repository (repeatable) that must hold the generated rules block, exactly
                  as `bin/vibe-verifier rules-doc` renders it for the rules the manifest at HEAD enables
                  ($VIBE_VERIFIER_MANIFEST, which the runner sets; this gate's own --rules and --pack
                  when it runs alone)

The block is how a repository's prose about its rules stays true: it is generated from the rule files,
never written by hand. A missing or different block is a finding naming the command that regenerates
it, so it blocks, and --soak reports it only, like any other finding. It is read from HEAD rather than
the base: the pull request that changes a rule regenerates the block with it.
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import textwrap
from collections import Counter

from _contract import CannotRun, Finding, added_files, changed_files, git, renamed, resolve_base, run_gate, tracked_files
from _tools import ensure

GATE = "repo-rules"
PACKS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "rules")
DEFAULT_RULES = ".vibe-verifier-rules"
CONFIG = "sgconfig.yml"  # at a rule directory's root: only its languageGlobs are read
MANIFEST = "VIBE_VERIFIER_MANIFEST"  # the runner sets it for every gate: the manifest file it read at HEAD
ADVISORY = ("warning", "info", "hint")  # severities printed but never blocking; unset and error block
# The generated rules block (`bin/vibe-verifier rules-doc`): found again by its first and last lines.
BEGIN = ("<!-- BEGIN vibe-verifier rules-doc: generated from the rule files by `bin/vibe-verifier rules-doc`; "
         "edit the rules and run it with --write, never this block -->")
END = "<!-- END vibe-verifier rules-doc -->"
BLOCK = re.compile(r"^<!-- BEGIN vibe-verifier rules-doc\b.*?^%s$" % re.escape(END), re.DOTALL | re.MULTILINE)
INTRO = ("`repo-rules` checks every pull request against these rules. A blocking rule fails a pull request that adds a "
         "finding to a file; an advisory one only reports it. A rule applies to the files of its language (`with` and `without` "
         "list the globs its directory's sgconfig.yml maps to that language and to others), narrowed by its `files` and "
         "`ignores` globs.")
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)
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
    although the head differs. With a base: every file the base has, at its base path and as the base
    has it, whatever the pull request did to it (edited, deleted, moved, renamed to anything), and the
    files the pull request added, except an sgconfig.yml: a mapping can narrow what rules see, so it
    applies once it merges. Without one: the head's."""
    prefix = directory + "/"
    if base is None:
        return {p[len(prefix):]: read(args.repo, p) for p in tracked if p.startswith(prefix) and is_yaml(p)}, []
    at_base = [p for p in git(args.repo, "ls-tree", "-r", "-z", "--name-only", base, "--", directory).split("\0") if is_yaml(p)]
    files, differs = {}, []
    for path in at_base:
        files[path[len(prefix):]] = text = show(args.repo, base, path)
        if path not in tracked or read(args.repo, path) != text:
            differs.append(path)
    for path in added_files(args.repo, base):
        if path == prefix + CONFIG:
            differs.append(path)
        elif path.startswith(prefix) and is_yaml(path) and path[len(prefix):] not in files:
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


def rule_sources(rules, packs, read_directory):
    """[(where, {path: bytes})] for every pack (from the catalog) and rule directory (default
    .vibe-verifier-rules), each directory as read_directory(directory) returns it."""
    sources = [("catalog:rules/%s/" % name, pack_files(name)) for name in packs or []]
    explicit = [os.path.normpath(d).replace(os.sep, "/") for d in rules or []]
    for directory in explicit or [DEFAULT_RULES]:
        files = read_directory(directory)
        if files:
            sources.append((directory + "/", files))
        elif explicit:
            raise CannotRun("--rules %s holds no rule files" % directory)
    for where, files in sources:
        if not any(not name.startswith("tests/") and name != CONFIG for name in files):
            raise CannotRun("%s holds no rules" % where)
    if not sources:
        raise CannotRun("no rules to run: add %s/, or pass --rules DIR or --pack NAME" % DEFAULT_RULES)
    return sources


def judged_directory(args, base, tracked):
    """read_directory for a run. A relative --rules is a directory of the repository, judged from the
    base. An absolute one lies outside it, such as one a wrapper writes, and is read as it is: a pull
    request cannot edit it."""
    def read_directory(directory):
        if os.path.isabs(directory):
            return directory_files(directory)
        files, differs = judged_files(args, base, directory, tracked)
        for path in differs:
            print("%s: %s is judged as it is at %s; this branch's change to it applies once it merges" % (GATE, path, base),
                  file=sys.stderr)
        return files
    return read_directory


def head_sources(repo, rules, packs):
    """The sources as the working tree has them, whatever the base has: what the rules block lists."""
    return rule_sources(rules, packs, lambda directory: directory_files(os.path.join(repo, directory)))


def assemble(sources, work):
    """Write each source into an ast-grep project of its own, <work>/<n>/ (rules/, tests/, and its
    sgconfig.yml as source-sgconfig.yml until configure() reads it); return {path under work: where
    it came from}."""
    origin = {}
    for number, (where, files) in enumerate(sources):
        for name, data in files.items():
            if name == CONFIG:
                assembled = "%d/source-%s" % (number, CONFIG)
            elif name.startswith("tests/"):
                assembled = "%d/%s" % (number, name)
            else:
                assembled = "%d/rules/%s" % (number, name)
            os.makedirs(os.path.dirname(os.path.join(work, assembled)), exist_ok=True)
            with open(os.path.join(work, assembled), "wb") as handle:
                handle.write(data)
            origin[assembled] = where + name
        for kind in ("rules", "tests"):
            os.makedirs(os.path.join(work, str(number), kind), exist_ok=True)
    return origin


def configure(work, count, docs, origin):
    """Write each project's sgconfig.yml: its rules and tests, plus the languageGlobs of the source's own
    sgconfig.yml, copied as written. Return the project roots."""
    roots = []
    for number in range(count):
        config, globs = "%d/source-%s" % (number, CONFIG), ""
        for keys, text in docs.get(config, []):
            if not keys and has_content(text):
                raise CannotRun("%s cannot be read; write it as a block mapping, one `key: value` per line" % origin[config])
            if "languageGlobs" in keys and not globs:
                globs = "languageGlobs:" + keys["languageGlobs"][0] + "\n"
        roots.append(os.path.join(work, str(number)))
        with open(os.path.join(roots[-1], CONFIG), "w", encoding="utf-8") as handle:
            handle.write("ruleDirs: [rules]\ntestConfigs:\n  - testDir: tests\n" + globs)
    return roots


def relabel(text, root, origin):
    """ast-grep's output for the project at root, with its temporary paths put back as the files they came from."""
    prefix = os.path.basename(root) + "/"
    for path in {root, os.path.realpath(root)}:  # a temporary directory may be reached through a symlink
        text = text.replace(path + os.sep, "")
    for assembled, where in origin.items():
        if assembled.startswith(prefix):
            text = text.replace(assembled[len(prefix):], where)
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


def documents(sg, work):
    """{path under work: [({top-level key: (value, [each item of its sequence, as a scalar])}, text)]}, one per
    YAML document. Only block mappings are read (`key: value` on its own line); a document of any other
    shape is refused."""
    result = subprocess.run([sg, "scan", "--inline-rules", READER, "--json=stream", "--", *sorted(os.listdir(work))],
                            cwd=work, capture_output=True, text=True)
    if result.returncode != 0:
        raise CannotRun("ast-grep could not read the rule files: %s" % (result.stderr.strip() or "no output"))
    hits = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]

    def spans(rule):
        return [(h["file"], h["range"]["byteOffset"]["start"], h["range"]["byteOffset"]["end"], h) for h in hits if h["ruleId"] == rule]
    keys = {}
    for path, start, end, hit in spans("key"):
        label = hit["metaVariables"]["single"]["KEY"]["text"]
        keys[(path, start, end)] = [scalar(label), hit["text"][len(label):].lstrip()[1:], []]  # value: past the key's colon
    for path, offset, _, hit in spans("item"):
        for (where, start, end), entry in keys.items():
            if where == path and start <= offset < end:
                entry[2].append(scalar(re.sub(r"^-(\s|$)", "", hit["text"])))  # a block item starts with its `- `
    docs = {}
    for path, start, end, hit in sorted(spans("doc"), key=lambda span: span[:3]):
        docs.setdefault(path, []).append(({key: (value, items) for (where, s, _), (key, value, items) in keys.items()
                                           if where == path and start <= s < end}, hit["text"]))
    return docs


def has_content(text):
    """Whether a YAML document holds anything but comments and document markers."""
    return any(line and not line.startswith("#") for line in (re.sub(r"^(---|\.\.\.)", "", raw).strip() for raw in text.splitlines()))


def value(keys, key):
    """A top-level key's scalar value, or "" when the document does not set it."""
    return scalar(keys[key][0]) if key in keys else ""


def read_rules(docs, origin):
    """{rule id: (project number, where, its top-level keys)} from documents(). Refuse a rule that cannot
    tell anyone what to do instead (no message) or that nothing proves (no rule test of its own
    directory with a valid and an invalid case)."""
    rules, cases, problems = {}, {}, []
    for path, entries in sorted(docs.items()):
        number, kind = path.split("/")[:2]
        if kind not in ("rules", "tests"):
            continue  # the source's sgconfig.yml, read by configure()
        for keys, text in entries:
            if "id" not in keys:
                if has_content(text):
                    problems.append("%s holds a document whose id cannot be read; write it as a block mapping, one "
                                    "`key: value` per line" % origin[path])
                continue
            rule_id = value(keys, "id")
            if kind == "rules":
                if rule_id in rules and rules[rule_id][0] != number:
                    problems.append("rule %s is defined in both %s and %s" % (rule_id, rules[rule_id][1], origin[path]))
                rules[rule_id] = (number, origin[path], keys)
            else:
                tally = cases.setdefault((number, rule_id), [0, 0])  # a test counts only for its own directory's rule
                tally[0] += len(keys.get("valid", ("", []))[1])
                tally[1] += len(keys.get("invalid", ("", []))[1])
    for rule_id, (number, where, keys) in sorted(rules.items()):
        valid, invalid = cases.get((number, rule_id), (0, 0))
        if not value(keys, "message"):
            problems.append("rule %s (%s) has no message; say what to write instead" % (rule_id, where))
        if not valid or not invalid:
            problems.append("rule %s (%s) has %d valid and %d invalid test cases under its directory's tests/; "
                            "it needs at least one of each" % (rule_id, where, valid, invalid))
    if problems:
        raise CannotRun("\n".join(problems))
    return rules


def verify(sg, work, origin, wheres):
    """Configure each project, run its rule tests, which must pass, and read its rules (read_rules).
    Return the project roots and the ids of the advisory rules."""
    docs = documents(sg, work)
    roots = configure(work, len(wheres), docs, origin)
    for root, where in zip(roots, wheres):  # each directory's tests run under its own languageGlobs
        tested = subprocess.run([sg, "test", "--config", CONFIG, "--skip-snapshot-tests", "--include-off", "--color", "never"],
                                cwd=root, capture_output=True, text=True)
        if tested.returncode != 0:
            raise CannotRun("the rule tests of %s do not pass:\n%s" % (where, relabel(tested.stdout + tested.stderr, root, origin)))
    rules = read_rules(docs, origin)
    return roots, {rule_id for rule_id, (_, _, keys) in rules.items() if value(keys, "severity") in ADVISORY}


def scan(sg, roots, root, paths):
    """Every finding of every project's rules in paths (relative to root), in a stable order."""
    hits = []
    for project in roots:
        for start in range(0, len(paths), CHUNK):
            result = subprocess.run([sg, "scan", "--config", os.path.join(project, CONFIG), "--off=unused-suppression",
                                     "--json=stream", "--", *paths[start:start + CHUNK]], cwd=root, capture_output=True, text=True)
            found = [json.loads(line) for line in result.stdout.splitlines() if line.strip()] if result.returncode in (0, 1) else []
            if result.returncode not in (0, 1) or (result.returncode == 1 and not found):
                raise CannotRun("ast-grep scan exited %d: %s" % (result.returncode, (result.stderr.strip() or "no output").splitlines()[-1]))
            hits += found
    return sorted(hits, key=lambda h: (h["file"], h["range"]["start"]["line"], h["range"]["start"]["column"], h["ruleId"]))


def finding(hit, advisory, context=""):
    text = "%s: %s%s" % (hit["ruleId"], hit["message"], context)
    if hit.get("note"):
        text += "\n  note: " + hit["note"].strip().replace("\n", "\n  ")
    return Finding(text, hit["file"], hit["range"]["start"]["line"] + 1, advisory=hit["ruleId"] in advisory)


def changed_lines(repo, merge_base, old, path):
    """The line numbers at HEAD that the pull request added or changed in path, or None if all of them
    are (the file is new). `old` is the file's path at the merge base."""
    if show(repo, merge_base, old) is None:
        return None
    diff = git(repo, "diff", "-U0", "--no-color", "--no-ext-diff", "--no-textconv", "%s:%s" % (merge_base, old), "HEAD:%s" % path)
    return {first + n for start, count in HUNK.findall(diff) for first in [int(start)] for n in range(int(count or 1))}


def mapped_globs(sg, docs, work):
    """{project number: {language casefolded: [globs]}} from each source sgconfig.yml's languageGlobs (the
    first, as configure() takes it). Its value, written out as a document of its own, is a mapping of
    language to a sequence of globs, which documents() reads like any rule file."""
    mappings = os.path.join(work, "language-globs")
    os.makedirs(mappings)
    for path, entries in docs.items():
        number, name = path.split("/")[:2]
        found = [keys["languageGlobs"][0] for keys, _ in entries if "languageGlobs" in keys]
        if name == "source-" + CONFIG and found:
            with open(os.path.join(mappings, number + ".yml"), "w", encoding="utf-8") as handle:
                handle.write(textwrap.dedent(found[0]))
    return {path[:-len(".yml")]: {language.casefold(): items for keys, _ in entries for language, (_, items) in keys.items()}
            for path, entries in documents(sg, mappings).items()}


def globs_text(globs):
    return ", ".join("`%s`" % glob for glob in globs)


def manifest_rules(path):
    """([--rules], [--pack]) of every repo-rules line of the manifest at path, in order and without repeats
    (its other arguments change no rule), or None when it has no repo-rules line."""
    parser = argparse.ArgumentParser(prog=GATE, add_help=False)
    add_arguments(parser)
    try:
        with open(path, encoding="utf-8") as handle:
            lines = [shlex.split(raw, comments=True) for raw in handle.read().splitlines()]
    except (OSError, ValueError) as error:
        raise CannotRun("cannot read the manifest %s: %s" % (path, error))
    enabled = [parser.parse_known_args(words[1:])[0] for words in lines if words[:1] == [GATE]]
    if not enabled:
        return None
    return (list(dict.fromkeys(d for line in enabled for d in line.rules or [])),
            list(dict.fromkeys(name for line in enabled for name in line.pack or [])))


def rules_doc(sg, sources):
    """The generated Markdown block: every rule of sources that runs (ast-grep never runs a rule whose
    severity is `off`), sorted by id, with whether it blocks as the gate judges it, its language with the
    globs its directory maps to that language and those it maps to another (a file they match is parsed as
    that other language, so the rule never sees it), its `files` and `ignores` globs, and its message."""
    with tempfile.TemporaryDirectory(prefix="vv-rules-doc-") as work:
        projects = os.path.join(work, "projects")
        origin = assemble(sources, projects)
        docs = documents(sg, projects)
        rules = read_rules(docs, origin)
        mapped = mapped_globs(sg, docs, work)
    # Blank lines after BEGIN and before END keep Markdown formatters (Prettier) from rewriting the block.
    lines = [BEGIN, "", INTRO, ""]
    for rule_id, (number, _, keys) in sorted(rules.items()):
        severity = value(keys, "severity")
        if severity == "off":
            continue
        language, mapping = value(keys, "language"), mapped.get(number, {})
        added = mapping.get(language.casefold(), [])
        moved = [glob for other, globs in sorted(mapping.items()) if other != language.casefold() for glob in globs]
        scope = [language + "".join(" %s %s" % (word, globs_text(globs)) for word, globs in (("with", added), ("without", moved)) if globs)]
        scope += ["%s %s" % (key, globs_text(keys[key][1])) for key in ("files", "ignores") if key in keys and keys[key][1]]
        lines.append("- `%s` (%s; %s): %s" % (rule_id, "advisory" if severity in ADVISORY else "blocking", "; ".join(scope),
                                              " ".join(value(keys, "message").split())))
    return "\n".join(lines + ["", END])


def _content(block):
    """Non-blank lines minus Markdown escapes: a formatter's blank lines or `\\_` never make a block stale."""
    return [re.sub(r"\\([\\`*_{}\[\]()#+\-.!|<>~])", r"\1", line.rstrip()) for line in block.splitlines() if line.strip()]


def stale_doc(repo, path, block):
    """A finding when the file at path in the repository lacks the generated block, or holds a different one."""
    fix = "regenerate it from the repository root with `<catalog>/bin/vibe-verifier rules-doc --repo . --manifest <manifest> --write %s`" % path
    try:
        with open(os.path.join(repo, path), encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except FileNotFoundError:
        text = ""
    found = BLOCK.search(text)
    if not found:
        return Finding("rules-doc: %s has no generated rules block; %s" % (path, fix), path)
    if _content(found.group(0)) != _content(block):
        return Finding("rules-doc: this rules block is not what the rule files at HEAD generate; %s" % fix,
                       path, text.count("\n", 0, found.start()) + 1)
    return None


def doc_rules(args):
    """The --rules and --pack the rules block lists: the repo-rules lines of the manifest the runner read at
    HEAD ($VIBE_VERIFIER_MANIFEST), which on a pull request may differ from this gate's own arguments
    (those are the base's); this gate's own when it runs alone or the manifest has no repo-rules line."""
    manifest = os.environ.get(MANIFEST)
    return (manifest_rules(manifest) if manifest else None) or (args.rules, args.pack)


def check(args):
    base = None if args.all else resolve_base(args.repo, args.base_ref)
    tracked = tracked_files(args.repo)
    sources = rule_sources(args.rules, args.pack, judged_directory(args, base, set(tracked)))
    sg = ensure("ast-grep")
    with tempfile.TemporaryDirectory(prefix="vv-rules-") as work:
        projects = os.path.join(work, "projects")
        roots, advisory = verify(sg, projects, assemble(sources, projects), [where for where, _ in sources])
        block = rules_doc(sg, head_sources(args.repo, *doc_rules(args))) if args.doc else None
        stale = [found for found in (stale_doc(args.repo, path, block) for path in args.doc or []) if found]
        if args.all:
            return stale + [finding(hit, advisory) for hit in scan(sg, roots, args.repo, sorted(tracked))]
        listed = set(tracked)
        head = scan(sg, roots, args.repo, sorted(p for p in changed_files(args.repo, base) if p in listed))
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
        before = Counter((hit["ruleId"], hit["file"]) for hit in scan(sg, roots, snapshot, at_base))
    now = Counter((hit["ruleId"], hit["file"]) for hit in head)
    findings = []
    for rule_id, path in sorted(key for key in now if now[key] > before[key]):
        hits = [hit for hit in head if (hit["ruleId"], hit["file"]) == (rule_id, path)]
        lines = changed_lines(args.repo, merge_base, moved.get(path, path), path)
        touched = [hit for hit in hits if lines is None or
                   lines & set(range(hit["range"]["start"]["line"] + 1, hit["range"]["end"]["line"] + 2))]
        counts = "in this file: %d at the merge base, %d now" % (before[(rule_id, path)], now[(rule_id, path)])
        if touched:
            findings += [finding(hit, advisory, " (%s)" % counts) for hit in touched]
        else:
            findings += [finding(hit, advisory, " (%s; none is on a line this pull request changed, so all are listed)" % counts)
                         for hit in hits]
    return stale + sorted(findings, key=lambda f: (f.path, f.line, f.message))


def add_arguments(parser):
    parser.add_argument("--rules", action="append", metavar="DIR", help="a rule directory in the repository (repeatable; "
                        "default %s when present)" % DEFAULT_RULES)
    parser.add_argument("--pack", action="append", metavar="NAME", help="a catalog pack, rules/NAME/ (repeatable)")
    parser.add_argument("--all", action="store_true", help="report every finding in every tracked file, with no ratchet")
    parser.add_argument("--doc", action="append", metavar="PATH", help="a file of the repository whose generated rules block "
                        "must match what `bin/vibe-verifier rules-doc` renders for the rules at HEAD (repeatable)")


if __name__ == "__main__":
    run_gate(GATE, __doc__, check, add_arguments)
