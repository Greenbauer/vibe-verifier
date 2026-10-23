"""Where a test starts and ends in a test file's lines, for the proof-audit gate (gates/proof_audit.py): a test's
declaration and title, and the lines its body covers, so a quoted assertion is tied to the test it sits in and a
hook or helper after that test never counts as part of it."""
import re

CLOSERS_ONLY = re.compile(r"^[)\]}>;,\s]*$")
STRING = re.compile(r"""(["'`])(?:\\.|(?!\1)[^\\])*\1""")
BLOCK_COMMENT = re.compile(r"/\*.*?\*/")
HEADER_LINES = 40  # how far a wrapped declaration may run (one parameter or fixture per line) before its body opens
REGEX_FOLLOWS = "(,=:[!&|?{};+-*%<>~^"  # what comes before a `/` that opens a regex literal, not a division
PY_OPENERS, JS_OPENERS = ('"""', "'''"), ("`", "/*")  # what opens a string or comment that can span lines
OPENS_ON_LINE = re.compile(r"(?:=>|\))\s*\{|\)\s*(?:->[^:]*)?:")  # `=> {`, `) {` or a def's `):` anywhere on a line
BODY_OPENS = re.compile(r"(?:=>|\))\s*\{?$|:$")  # `=> {`, `=>`, `) {`, or a Python def's closing `:`
# A test declaration: a JS test call (test(...), it(...), test.only(...), test.each(...)(...)), `void` first when a
# type-aware lint forbids a floating promise (`void it(...)`), or a Python test
# function. Its title is the call's first string literal, or the function's name. Only the modifiers that still
# register a test count: test.setTimeout(...), test.slow(), test.step(...), test.describe(...) and the hooks are
# calls inside or around tests, and taking one for the declaration would tie an assertion to no test.
DECLARATION = re.compile(r"^\s*(?:(?:void\s+)?(?:it|test|specify)(?:\.(?:only|skip|concurrent|serial|sequential|fixme|fail|failing|todo"
                         r"|each\([^)]*\)))*\s*\(|(?:async\s+)?def\s+test\w*\s*\()")
PYTHON_TEST = re.compile(r"^\s*(?:async\s+)?def\s+(test\w*)\s*\(")
TITLE = re.compile(r"""\(\s*(["'`])((?:\\.|(?!\1)[^\\])*)\1""")


def closes_on_its_line(line):
    """Whether a JS test declaration line also closes its call: a one-line test ends `)` or `);`, read with its
    block comments dropped, and again with its strings and a trailing `//` comment dropped too (a multi-line
    declaration ends with the `{`, `(` or `=>` that opens its body)."""
    raw = BLOCK_COMMENT.sub("", line).rstrip()
    bare = STRING.sub("", raw).split("//", 1)[0].rstrip()
    if bare.endswith(("{", "(", "=>", ",", "[")):
        return False  # it opens a body on the next lines, whatever a trailing comment ends with
    uncommented = raw.rsplit(" //", 1)[0].rstrip()
    return any(text.endswith((")", ");")) for text in (raw, uncommented, bare))


def bracket_depth(text):
    """Openers minus closers on a line, string literals and a trailing `//` comment left out."""
    text = STRING.sub("", text).split("//", 1)[0]
    return sum(text.count(c) for c in "([{") - sum(text.count(c) for c in ")]}")


def declared_title(declaration):
    """The title a declaration gives its test: a Python test function's name, or the string literal a JS
    test call opens with; None when the title is not a literal (a variable)."""
    python = PYTHON_TEST.search(declaration)
    if python:
        return python.group(1)
    literal = TITLE.search(declaration)
    return literal.group(2) if literal else None


def declaration_above(lines, number):
    """The index of the nearest test declaration above line `number`, or None: the test an assertion there sits
    in, when it is inside that test's body."""
    return next((index for index in range(number - 2, -1, -1) if DECLARATION.match(lines[index])), None)


def test_body(lines, title):
    """(first, last), the 1-based lines of the one test titled `title`: its declaration down to the line that closes
    it. None when no test, or more than one, carries that title. A body never reaches past first_dedent (a JS test's
    closing `});`, or a Python test's dedent); within that it also ends where the bracket count closes the
    declaration's call, on the declaration's own line for a one-line test."""
    starts = [index for index, line in enumerate(lines)
              if DECLARATION.match(line) and declared_title("\n".join(lines[index:index + 3])) == title]
    return (starts[0] + 1, body_end(lines, starts[0])) if len(starts) == 1 else None


def header_end(lines, start):
    """The index of the line a declaration's body opens on: the declaration's own line, or a later one when the
    declaration wraps (`async ({` / `page` / `}, testInfo) => {`, or a Python def whose `):` closes its arguments),
    so a wrapped line at the declaration's indent is not taken for the end of the test. The search never runs past
    the line that closes the declaration's call, so an expression-arrow test (`() => expect(...),` / `30_000,` /
    `);`) ends at its own `);`, never at a later hook's `=> {`."""
    comment = "#" if PYTHON_TEST.match(lines[start]) else "//"
    bare = [STRING.sub("", BLOCK_COMMENT.sub("", line)).split(comment, 1)[0].rstrip()
            for line in lines[start:start + HEADER_LINES]]
    if OPENS_ON_LINE.search(bare[0]):
        return start  # the body opens on the declaration's own line, whatever follows it there
    depth = 0
    for offset, text in enumerate(bare):
        if BODY_OPENS.search(text):
            return start + offset
        depth += bracket_depth(text)
        if depth <= 0:
            return start + offset  # the call closed with no `{` body (an expression arrow): it ends here
    return start


def first_dedent(lines, start):
    """The index of the first line after the declaration's header indented no deeper than the declaration, or
    len(lines). Blank lines, comment lines, and lines that start inside a multi-line string (a JS template literal
    or block comment, a Python triple-quoted string: an inline fixture, a SQL string) never end the test, whatever
    their indentation."""
    python = bool(PYTHON_TEST.match(lines[start]))
    indent = len(lines[start]) - len(lines[start].lstrip())
    header, inside = header_end(lines, start), ()
    for index in range(start, len(lines)):
        line = lines[index]
        text = line.strip()
        if (index > header and not inside and text and not text.startswith("#" if python else ("//", "/*"))
                and len(line) - len(line.lstrip()) <= indent):
            return index
        inside = open_at_end(line, inside, python)
    return len(lines)


def open_at_end(line, inside, python):
    """The multi-line strings, comments and template `${...}` expressions still open at the end of `line`, innermost
    last, given those open at its start (() for none): a Python triple-quoted string, or a JS template literal, block
    comment, or the code of a `${...}` (closed by its `}`), where a template literal can open again. A string that
    opens and closes on the line, and a line comment, change nothing."""
    index = 0
    while index < len(line):
        if inside and inside[-1] != "}":
            index, inside = past_close(line, index, inside)
        else:
            index, inside = past_code(line, index, inside, python)
    return inside


def past_close(line, index, inside):
    """One step through the string or comment inside[-1] closes: (the next index, `inside` with it popped once
    closed, or with a `${...}` pushed when a template literal opens one)."""
    closer = inside[-1]
    if line[index] == "\\" and closer != "*/":
        return index + 2, inside
    if closer == "`" and line.startswith("${", index):
        return index + 2, inside + ("}",)
    if line.startswith(closer, index):
        return index + len(closer), inside[:-1]
    return index + 1, inside


def past_code(line, index, inside, python):
    """One step through code, at the top level or in a `${...}` (inside[-1] == "}"), where a `{` nests and the `}`
    that matches closes it: (the next index, `inside` with what opened or closed here pushed or popped)."""
    if inside and line[index] in "{}":
        return index + 1, inside + ("}",) if line[index] == "{" else inside[:-1]
    index, opened = past_token(line, index, python)
    return index, inside + (opened,) if opened else inside


def past_token(line, index, python):
    """One step through code: (the next index, the delimiter that closes a multi-line string or comment that opens
    here, or None). A line comment runs to the line's end, and a one-line string is stepped over whole."""
    opener = next((mark for mark in (PY_OPENERS if python else JS_OPENERS) if line.startswith(mark, index)), None)
    if opener:
        return index + len(opener), "*/" if opener == "/*" else opener
    if line.startswith("#" if python else "//", index):
        return len(line), None
    if line[index] in "'\"":
        return string_end(line, index), None
    if not python and line[index] == "/" and regex_can_start(line, index):
        return regex_end(line, index), None
    return index + 1, None


def regex_can_start(line, index):
    """Whether the `/` at `index` opens a JS regex literal rather than dividing: what comes before it is the line's
    start, `return`, `typeof`, or one of REGEX_FOLLOWS (an operator, an opening bracket, `}`, `;`, `:`, `?` or a
    comma). A regex after `)` or `await` reads as a division."""
    before = line[:index].rstrip()
    return not before or before[-1] in REGEX_FOLLOWS or before.endswith(("return", "typeof"))


def regex_end(line, index):
    """The index just past the regex literal that opens at `index`: its closing `/` outside a [...] class, then its
    flags (past the line's end if it never closes there)."""
    index, in_class = index + 1, False
    while index < len(line):
        char = line[index]
        if char == "\\":
            index += 2
            continue
        if char == "/" and not in_class:
            index += 1
            while index < len(line) and line[index].isalpha():
                index += 1
            return index
        in_class = (in_class or char == "[") and char != "]"
        index += 1
    return index


def string_end(line, index):
    """The index just past the one-line quoted string that opens at `index` (past the line's end if it never closes
    there)."""
    quote, index = line[index], index + 1
    while index < len(line) and line[index] != quote:
        index += 2 if line[index] == "\\" else 1
    return index + 1


def body_end(lines, start, brackets=True):
    """The 1-based last line of the test declared at index `start` (see test_body). Without `brackets` it is the
    indentation bound alone (and a one-line test's own line): looser, so a bracket the count misreads in a
    formatted test does not cut a body short and turn away an assertion that is in it."""
    dedent = first_dedent(lines, start)
    if PYTHON_TEST.match(lines[start]):
        return dedent
    if dedent < len(lines) and CLOSERS_ONLY.match(lines[dedent].strip()):
        dedent += 1
    if closes_on_its_line(lines[start]):
        return start + 1  # a one-line test, whatever a bracket inside a regex or comment says
    if not brackets:
        return dedent
    depth = 0
    for index in range(start, dedent):
        depth += bracket_depth(lines[index].strip())
        if depth <= 0:
            return index + 1
    return dedent
