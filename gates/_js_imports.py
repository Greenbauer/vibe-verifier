"""Whether a JS or TS file loads a module when it runs, not only names its types, for regression-test-fails-on-base
(gates/_regression_frameworks.py). Read by pattern, not parsed, over the file with its comments dropped and each string,
template and regex literal masked, so a specifier that appears only in a comment (a JSDoc `import("...")` type) or inside
another string (a package.json key, a message, a fixture's source text) is not an import.

An import loads the module unless it only names types: `import type ...` and `export type ... from`, a `{ ... }` whose
every name is marked `type` or capitalized, and an `import("...")` read for a capitalized member (`import("m").Page`) or
under `typeof`. A capitalized name is taken for a type because neither module this is asked about exports a capitalized
value: @playwright/test's runtime exports are `test`, `expect`, `devices`, `chromium` and the like, its classes and
interfaces (`Page`, `Locator`, `TestInfo`) are types only, and the same holds for node:test. A default or namespace import,
`import "m"`, `export * from "m"` and `require("m")` load it.
"""
import re

# The last significant character or word before a `/` that starts a regex literal rather than a division.
REGEX_AFTER = re.compile(r"(?:^|[(,=:\[!&|?{};+\-*%<>~^]|\b(?:return|typeof|case|do|else|in|of|void|yield|await|delete|throw|new))\s*$")
COMMENT = re.compile(r"//[^\n]*|/\*.*?(?:\*/|\Z)", re.S)
# A quoted string to its closing quote, or to the end of its line when it has none.
STRING = re.compile(r"""'(?:\\.|[^'\\\n])*'?|"(?:\\.|[^"\\\n])*"?""")
# A regex literal: escapes and `[...]` classes, which may hold a `/`, then its flags.
REGEX = re.compile(r"/(?:\\.|\[(?:\\.|[^\]\\\n])*\]|[^/\\\n\[])+/\w*")
# A clause of names between `import` or `export` and `from`: identifiers, braces, commas, `*`, `as`, `type`.
STATIC = re.compile(r'\b(import|export)\s+([\w$\s{},*]*?)\s*\bfrom\s*"#(\d+)"')
BARE = re.compile(r'\bimport\s*"#(\d+)"')
CALL = re.compile(r'(\btypeof\s+)?\b(import|require)\s*\(\s*"#(\d+)"\s*\)(\s*\.\s*[A-Z])?')


def template_end(text, start):
    """The index after the template literal that starts at `start`, its `${...}` expressions skipped by brace depth."""
    index, depth = start + 1, 0
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 1
        elif text.startswith("${", index):
            depth, index = depth + 1, index + 1
        elif char == "}" and depth:
            depth -= 1
        elif char == "`" and not depth:
            return index + 1
        index += 1
    return len(text)


def literal_end(text, start):
    """The index after the string, template or regex literal that starts at `start`, or None when a `/` starts none
    (a division after all)."""
    if text[start] == "`":
        return template_end(text, start)
    found = (REGEX if text[start] == "/" else STRING).match(text, start)
    return found.end() if found else None


def masked(text):
    """(the text with each comment blanked and each literal replaced by "#<n>", [each literal's contents])."""
    out, literals, index = [], [], 0
    while index < len(text):
        comment = COMMENT.match(text, index)
        if comment:
            out.append(" ")
            index = comment.end()
            continue
        char = text[index]
        starts = char in "'\"`" or (char == "/" and REGEX_AFTER.search("".join(out[-40:])))
        end = literal_end(text, index) if starts else None
        if end:
            literals.append(text[index + 1:end - 1])
            out.append('"#%d"' % (len(literals) - 1))
            index = end
        else:
            out.append(char)
            index += 1
    return "".join(out), literals


def loads_names(clause):
    """Whether an import or export clause binds a value: a default or namespace binding, `*`, an empty `{}` (an import
    for its side effects), or a `{ ... }` name neither marked `type` nor capitalized."""
    clause = clause.strip()
    if re.match(r"type\s+[\w${*]", clause):
        return False
    braces = re.search(r"\{([^}]*)\}", clause)
    if not braces or re.sub(r"\{[^}]*\}", "", clause).strip(" ,\n\t"):
        return True
    names = [name.strip() for name in braces.group(1).split(",") if name.strip()]
    return not names or any(not re.match(r"type\s", name) and not name[0].isupper() for name in names)


def imports_at_runtime(text, module):
    """Whether the file's code loads `module` when it runs (see the module docstring)."""
    code, literals = masked(text)
    named = {str(index) for index, value in enumerate(literals) if value == module}
    if any(found.group(3) in named and loads_names(found.group(2)) for found in STATIC.finditer(code)):
        return True
    if any(found.group(1) in named for found in BARE.finditer(code)):
        return True
    return any(found.group(3) in named and (found.group(2) == "require" or not (found.group(1) or found.group(4)))
               for found in CALL.finditer(code))
