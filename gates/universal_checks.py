#!/usr/bin/env python3
"""Hold the code to a repository's standing rules: every member of a population conforms.

A pure gate. The rules live in one Markdown file (default `ci/universal-checks.md`), each under a
`Universal checks:` heading (up to three `#`, bold, any case, optional colon), one bullet per rule:

    Universal checks:
    - `<population regex>` in `<path glob>` conforms to `<conforms regex>`

Every match of the population regex in a tracked file the glob selects is a MEMBER. A member is
graded on the call argument its match ends in when it ends in a call, or is followed by one: for a
call-shape population (`\\.route\\(`) the FIRST argument, the matcher and not the handler body; a
population that consumes leading arguments (`\\.on\\(\\s*"response"\\s*,`) grades the next one. Any
other member is graded on the rest of its line. It conforms when that text matches the conforms
regex; exemptions are alternatives in it. Regexes are Python `re` with MULTILINE. The glob is
`fnmatch` over the repository-relative path, so `*` also crosses `/` (`e2e/*.spec.ts` matches at any
depth). Comments in the `//` and `/* */` forms are blanked first, so a match inside one is not a
member, and string, template and regex literals are skipped when finding the argument; a comment in
another language's form still counts. A wrapped bullet (an indented line under it) joins it.

Write the population as every place the rule governs, by the shape of the code that does the
governed thing, never by the wrong forms a ticket happened to mention: "every API interceptor goes
through the shared helper" lists every method of that API that can intercept.

Judged from the merge base. The rules are read from the file as the merge base of the base ref and
HEAD has it, so a pull request that edits, weakens or deletes a rule is still judged by it. When the
head's copy differs from that one (or the base has none), the head's copy is graded too, at the
head, so a rule a pull request adds is proven before it merges.

Exit 1 when a member does not conform. Exit 2, even under --soak, when nothing trustworthy can be
said: no rules file at the merge base or the head; a copy that parses to no rule, holds a malformed
rule or a heading with no rule under it, or has a backticked bullet outside a section (a note
between bullets ends the section above it); a regex that does not compile; a glob that matches no
tracked file; or a population that matches nothing. An empty universal is a typo or a rule whose
code is gone, never a pass: retire a rule by removing its line first, then the code it governed.

    --rules PATH     the rules file, relative to the repository (default ci/universal-checks.md)
"""
import fnmatch
import os
import re
import subprocess
from typing import NamedTuple

from _contract import CannotRun, Finding, git, resolve_base, run_gate, tracked_files

GATE = "universal-checks"
HEADING = re.compile(r"^\s*#{0,3}\s*(\*\*)?universal checks?(\*\*)?\s*:?\s*(\*\*)?\s*:?\s*$", re.I)
RULE = re.compile(r"^\s*[-*]\s+`([^`]+)`\s+in\s+`([^`]+)`\s+conforms to\s+`([^`]+)`\s*\.?\s*$")
BULLET = re.compile(r"^\s*[-*]\s+")
STRAY = re.compile(r"^\s*[-*]\s+`")


class Rule(NamedTuple):
    population: str
    glob: str
    conforms: str
    origin: str  # which copy of the file it came from, for messages


def sections(text):
    """[(heading, [bullet texts])] for every `Universal checks` heading; a wrapped bullet joins the one above."""
    found, lines, i = [], text.splitlines(), 0
    while i < len(lines):
        if not HEADING.match(lines[i]):
            i += 1
            continue
        heading, bullets = lines[i].strip(), []
        i += 1
        while i < len(lines) and (not lines[i].strip() or BULLET.match(lines[i]) or (bullets and lines[i][:1].isspace())):
            if BULLET.match(lines[i]):
                bullets.append(lines[i].strip())
            elif lines[i].strip():
                bullets[-1] += " " + lines[i].strip()
            i += 1
        found.append((heading, bullets))
    return found


def parse(text, origin):
    """The rules in one copy of the file, or exit 2 naming everything wrong with it."""
    rules, malformed, problems = [], [], []
    for heading, bullets in sections(text):
        if not bullets:
            problems.append("%s: `%s` has no rule under it" % (origin, heading))
        for bullet in bullets:
            match = RULE.match(bullet)
            if match:
                rules.append(Rule(*match.groups(), origin=origin))
            else:
                malformed.append(bullet)
    problems += ["%s: malformed rule (write - `<population>` in `<glob>` conforms to `<conforms>`): %s" % (origin, bullet)
                 for bullet in malformed]
    stray = sum(1 for line in text.splitlines() if STRAY.match(line)) - len(rules) - len(malformed)
    if stray > 0:
        problems.append("%s: %d backticked bullet(s) outside a Universal checks section; a note between bullets "
                        "ends the section above it" % (origin, stray))
    if not rules and not problems:
        problems.append("%s: no rule in it (a `Universal checks:` heading with one bullet per rule)" % origin)
    if problems:
        raise CannotRun("; ".join(problems))
    return rules


# A `/` opens a regex literal, not a division, when the last significant character before it is one of
# these (or the keyword `return`): the heuristic JavaScript tokenizers use without a full parser.
REGEX_PRECEDERS = set("(,=:[!&|?{};>")
RETURN_BEFORE = re.compile(r"(?:^|[^\w$])return\s*$")


def comment_end(text, i):
    """Where the `//` or `/* */` comment starting at `i` ends (its newline is kept)."""
    closer = "\n" if text[i + 1] == "/" else "*/"
    stop = text.find(closer, i + 2)
    return len(text) if stop == -1 else stop + (0 if closer == "\n" else 2)


def literal_end(text, i, regex):
    """The index of the quote, backtick or slash closing the literal opened at `i` (or where a
    non-template literal hits a newline unclosed)."""
    opener, k, in_class = text[i], i + 1, False
    while k < len(text):
        char = text[k]
        if char == "\\":
            k += 2
            continue
        if regex and char in "[]":
            in_class = char == "["
        elif (char == opener and not in_class) or (char == "\n" and opener != "`"):
            return k
        k += 1
    return k


def opens_regex(text, i, last):
    """Whether the `/` at `i`, after the significant character `last`, opens a regex literal."""
    return text[i] == "/" and (last in REGEX_PRECEDERS or last == "" or bool(RETURN_BEFORE.search(text[max(0, i - 40):i])))


def blank(text, targets, start, stop):
    """Turn every character of text[start:stop] but a newline into a space, in each list of `targets`."""
    for k in range(start, min(stop, len(text))):
        if text[k] != "\n":
            for target in targets:
                target[k] = " "


def lex(text):
    """(code, shape), each as long as `text` with its newlines kept. `code` blanks comments; `shape`
    also blanks the inside of string, template and regex literals, so counting parentheses and commas
    sees only real syntax."""
    code, shape = list(text), list(text)
    i, last = 0, ""
    while i < len(text):
        regex = opens_regex(text, i, last)
        if text.startswith(("//", "/*"), i):
            stop = comment_end(text, i)
            blank(text, (code, shape), i, stop)
            i = stop
        elif text[i] in "'\"`" or regex:
            stop = literal_end(text, i, regex)
            blank(text, (shape,), i + 1, stop)
            last, i = text[i], stop + 1
        else:
            last = last if text[i].isspace() else text[i]
            i += 1
    return "".join(code), "".join(shape)


def call_opening(shape, start, end):
    """The `(` of the call a member's match ends in: the last one inside it, else one right after it."""
    opening = shape.rfind("(", start, end)
    if opening == -1:
        after = re.match(r"\s*\(", shape[end:])
        opening = end + after.end() - 1 if after else -1
    return opening


def graded_text(code, shape, start, end):
    """What a member is graded on: the call argument its match ends in, or the rest of its line."""
    opening = call_opening(shape, start, end)
    if opening == -1:
        line_end = code.find("\n", end)
        return code[start:] if line_end == -1 else code[start:line_end]
    depth, arg_start = 0, opening + 1
    for i in range(opening, len(shape)):
        char = shape[i]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                return code[arg_start:i]
        elif char == "," and depth == 1:
            if i >= end:
                return code[arg_start:i]
            arg_start = i + 1
    return code[arg_start:]


def compiled(rule):
    try:
        return re.compile(rule.population, re.MULTILINE), re.compile(rule.conforms, re.MULTILINE)
    except re.error as error:
        raise CannotRun("%s: a regex in `%s` in `%s` does not compile: %s" % (rule.origin, rule.population, rule.glob, error))


def grade(repo, rule, tracked):
    """[Finding] for the members of `rule` that do not conform, or exit 2 when it has no members."""
    population, conforms = compiled(rule)
    selected = [path for path in tracked if fnmatch.fnmatch(path, rule.glob)]
    if not selected:
        raise CannotRun("%s: `%s` matches no tracked file" % (rule.origin, rule.glob))
    members, findings = 0, []
    for path in selected:
        try:
            with open(os.path.join(repo, path), encoding="utf-8") as handle:
                code, shape = lex(handle.read())
        except (OSError, UnicodeDecodeError):
            continue
        for match in population.finditer(code):
            members += 1
            if not conforms.search(graded_text(code, shape, match.start(), match.end())):
                findings.append(Finding("does not conform to `%s` (rule `%s` in `%s`, from %s)"
                                        % (rule.conforms, rule.population, rule.glob, rule.origin),
                                        path, code.count("\n", 0, match.start()) + 1))
    if not members:
        raise CannotRun("%s: `%s` matches nothing in `%s` (an empty universal is a typo, or its code is gone: "
                        "remove the rule first)" % (rule.origin, rule.population, rule.glob))
    print("%s: `%s` in `%s` (%s): %d member(s), %d not conforming" % (GATE, rule.population, rule.glob, rule.origin,
                                                                     members, len(findings)))
    return findings


def at_merge_base(repo, path, base):
    """The rules file as the merge base of `base` and HEAD has it, or None when it has none."""
    point = git(repo, "merge-base", base, "HEAD").strip()
    shown = subprocess.run(["git", "-C", repo, "show", "%s:%s" % (point, path)], capture_output=True, text=True)
    return (point, shown.stdout) if shown.returncode == 0 else (point, None)


def check(args):
    path = args.rules
    point, base_text = at_merge_base(args.repo, path, resolve_base(args.repo, args.base_ref))
    head_file = os.path.join(args.repo, path)
    head_text = None
    if os.path.isfile(head_file):
        with open(head_file, encoding="utf-8") as handle:
            head_text = handle.read()
    if base_text is None and head_text is None:
        raise CannotRun("no %s at the merge base or the head (one `Universal checks:` heading, one bullet per rule)" % path)
    rules = parse(base_text, "%s at the merge base %s" % (path, point[:12])) if base_text is not None else []
    if head_text is not None and head_text != base_text:
        head = parse(head_text, "%s at the head" % path)
        known = {(r.population, r.glob, r.conforms) for r in rules}
        rules += [rule for rule in head if (rule.population, rule.glob, rule.conforms) not in known]
    tracked = tracked_files(args.repo)
    findings = []
    for rule in rules:
        findings += grade(args.repo, rule, tracked)
    return findings


def add_arguments(parser):
    parser.add_argument("--rules", metavar="PATH", default="ci/universal-checks.md")


if __name__ == "__main__":
    run_gate(GATE, __doc__, check, add_arguments)
