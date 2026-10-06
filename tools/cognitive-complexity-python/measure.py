"""Print every Python function over the cognitive-complexity limit, for the cognitive-complexity gate.

Run by the pinned venv's interpreter: measure.py <root> <limit> <file>... prints one JSON object,
{path: [[line, message], ...]}, with paths relative to <root>. A file that does not parse is
printed to stderr as `could not parse <path>: <reason>` and exits 3, so the gate cannot pass it.

complexipy skips a function marked `# noqa: complexipy` or `# complexipy: ignore` (any case). The
gate ignores a consumer's own suppressions on both languages (the eslint side runs with an empty
suppressions file), so every spelling of the name is neutralized before measuring: an identifier
swap that keeps the syntax and every line number.
"""
import json
import os
import re
import sys

import complexipy

MARKER = re.compile("complexipy", re.IGNORECASE)


def main(root, limit, files):
    found = {}
    for path in files:
        with open(os.path.join(root, path), encoding="utf-8", errors="replace") as handle:
            source = MARKER.sub("vv_neutral", handle.read())
        try:
            functions = complexipy.code_complexity(source).functions
        except ValueError as error:
            print("could not parse %s: %s" % (path, str(error).split("\n")[0]), file=sys.stderr)
            return 3
        for function in functions:
            if function.complexity > limit:
                name = function.name.replace("::", ".")
                found.setdefault(path, []).append([function.line_start, "Refactor %s to reduce its Cognitive Complexity from %d to the %d allowed."
                                                   % (name, function.complexity, limit)])
    print(json.dumps(found))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], int(sys.argv[2]), sys.argv[3:]))
