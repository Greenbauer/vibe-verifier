"""Python files for the changed-code-mutation gate: mutmut 3.8, pinned in tools/changed-code-mutation-python.

mutmut runs the project's own pytest in the project's own interpreter (--python). Its package and every
dependency it needs except pytest live in a cached venv made from that interpreter and are appended to the
end of sys.path, so the project's own packages always win. It works on a copy of the project's tracked
files, under the gate's [tool.mutmut] table in place of any the project has:

1. Generate: mutmut writes a mutant for each change it can make in every function of the changed files
   (only_mutate), then runs the whole suite once to learn which tests reach which function. It is asked
   for a mutant name that matches nothing, so it stops there by its own assertion (exit 1); this step is
   judged by the files it wrote, never by that exit code. With no coverage stats written, either the
   suite failed or no test reached a mutated function (mutmut exits 1 for both): the gate runs the
   project's pytest once itself, and a passing suite makes every mutant "not covered".
2. Select: each mutant sits on the source line where it first differs from the copy of the function it
   was made from (the .spans file gives both copies' lines in mutmut's output; ast gives the function's
   def line in the source). Only mutants on the changed lines are kept.
3. Run: mutmut runs just those, each against the tests that reach its function.

mutmut makes no mutant in a decorated function, a method of a nested class, or a line marked
`# pragma: no mutate`; changed lines there are noted as left out and never scored.
"""
import ast
import json
import os
import re
import shutil
import sys
import tempfile
import time

from _contract import CannotRun, git
from _tools import ensure_python_tool, run_tool

TOOL = "changed-code-mutation-python"
NOTHING = "vv-select-no-mutant"
KILLED = {1, 3}
UNDETECTED = {0: "survived: every test still passed", 5: "not covered: no test runs this code",
              33: "not covered: no test runs this code"}
EXCLUDED = {34: "skipped", 35: "suspicious: its tests ran far slower than without it", 36: "timed out",
            24: "timed out", -24: "timed out", 152: "timed out", 37: "caught by the type check"}
FUNCTION = (ast.FunctionDef, ast.AsyncFunctionDef)
CLASS_MARK = "\u01c1"  # mutmut's separator in a method's name: x<mark>Class<mark>method
PRAGMA = re.compile(r"#\s*pragma:.*no mutate")
BOOT = "import sys; sys.path.append(%r); sys.argv = ['mutmut'] + sys.argv[1:]; from mutmut.__main__ import cli; cli()"


def note(text):
    print("changed-code-mutation: %s" % text, file=sys.stderr)


def interpreter(python, repo):
    """The project's interpreter: a name on PATH, or a path relative to the repository."""
    if os.sep in python and not os.path.isabs(python):
        python = os.path.join(repo, python)
    found = shutil.which(python)
    if not found:
        raise CannotRun("--python %s is not an interpreter on PATH or in the repository" % python)
    probe = run_tool([found, "-c", "import sys, pytest; print('%d.%d' % sys.version_info[:2])"], None, 60, "python")
    if probe[0] != 0:
        raise CannotRun("no pytest is installed for %s: install the project's dependencies before this gate, which "
                        "runs the project's own pytest" % python)
    version = probe[1].strip().splitlines()[-1]
    if tuple(int(part) for part in version.split(".")) < (3, 10):
        raise CannotRun("%s is Python %s; the pinned mutmut needs 3.10 or later" % (python, version))
    return found


def members(tree):
    """(node, mutmut's name for it, a readable name) for each top-level function and each class's direct members."""
    for node in tree.body:
        if isinstance(node, FUNCTION):
            yield node, "x_" + node.name, node.name
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, FUNCTION + (ast.ClassDef,)):
                    yield item, "x%s%s%s%s" % (CLASS_MARK, node.name, CLASS_MARK, item.name), "%s.%s" % (node.name, item.name)


def functions(source):
    """({mutmut's name for each function it mutates: its def line}, [(name, first, last) of each it skips])."""
    mutated, skipped = {}, []
    for node, key, name in members(ast.parse(source)):
        if isinstance(node, FUNCTION) and not node.decorator_list:
            mutated[key] = node.lineno
        else:
            first = node.decorator_list[0].lineno if node.decorator_list else node.lineno
            skipped.append((name, first, node.end_lineno))
    return mutated, skipped


def from_def(lines, first, last):
    """Lines first..last (1-based, inclusive) of a span, from its def line on (a span starts with blank lines)."""
    span = lines[first - 1:last]
    start = next((i for i, line in enumerate(span) if line.lstrip().startswith(("def ", "async def "))), None)
    if start is None:
        raise CannotRun("mutmut's span %d-%d holds no def line" % (first, last))
    return span[start:]


def place(path, source, mutated_source, spans):
    """{mutant (its name without the module): (source line, original text, mutated text)}."""
    defs, _ = functions(source)
    source_lines, lines = source.splitlines(), mutated_source.splitlines()
    placed = {}
    for key, (first, last) in spans.items():
        function, _, suffix = key.rpartition("__mutmut_")
        if suffix == "orig":
            continue
        if function + "__mutmut_orig" not in spans or function not in defs:
            raise CannotRun("could not find the function mutmut's mutant %s copies in %s" % (key, path))
        original = from_def(lines, *spans[function + "__mutmut_orig"])
        mutant = from_def(lines, first, last)
        mutant[0] = mutant[0].replace(key, function + "__mutmut_orig", 1)
        line = defs[function]
        if source_lines[line:line + len(original) - 1] != original[1:]:
            raise CannotRun("mutmut's copy of %s in %s does not match the source; no line for its mutants" % (function, path))
        offset = next((i for i, (a, b) in enumerate(zip(original, mutant)) if a != b), min(len(original), len(mutant)) - 1)
        placed[key] = (line + offset, original[offset].strip(), mutant[offset].strip())
    return placed


def write_config(workspace, files):
    """The gate's [tool.mutmut] table, in place of any the project's pyproject.toml has."""
    path = os.path.join(workspace, "pyproject.toml")
    kept, skipping = [], False
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as handle:
            for line in handle.read().splitlines():
                header = re.match(r"\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(?:#.*)?$", line)
                if header:
                    skipping = re.match(r"tool\.mutmut(\.|$)", header.group(1).replace('"', "").replace("'", "")) is not None
                if not skipping:
                    kept.append(line)
    roots = sorted({path.split("/", 1)[0] for path in files})
    table = {
        "source_paths": roots,
        "only_mutate": sorted(re.sub(r"[\[*?]", lambda char: "[%s]" % char.group(0), path) for path in files),
        "also_copy": sorted(name for name in os.listdir(workspace) if name != "mutants"),
    }
    kept += ["", "[tool.mutmut]"] + ["%s = %s" % (key, json.dumps(value)) for key, value in table.items()]
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(kept) + "\n")


def copy_project(repo, prefix, workspace):
    for path in git(repo, "ls-files", "-z", "--", prefix or ".").split("\0"):
        if not path or not path.startswith(prefix):
            continue
        source, target = os.path.join(repo, path), os.path.join(workspace, path[len(prefix):])
        if os.path.lexists(source) and not os.path.isdir(source):
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(source, target, follow_symlinks=False)


def read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def left_out(path, source, spans):
    """Notes for the changed lines mutmut makes no mutant on."""
    _, skipped = functions(source)
    changed = {line for first, last in spans for line in range(first, last + 1)}
    for name, first, last in skipped:
        hit = sorted(changed & set(range(first, last + 1)))
        if hit:
            note("%s:%d: left out: mutmut does not mutate %s (decorated, or in a nested class)" % (path, hit[0], name))
    for number, line in enumerate(source.splitlines(), 1):
        if number in changed and PRAGMA.search(line):
            note("%s:%d: left out by its `# pragma: no mutate` comment" % (path, number))


def collect(boot, python, workspace, deadline, timeout):
    """Step 1, generate: whether mutmut wrote coverage stats. Without them, pytest decides between a failing
    suite (CannotRun) and one no test of which reaches the changed functions."""
    _, output = run_tool(boot + [NOTHING], workspace, deadline - time.monotonic(), "mutmut", budget=timeout)
    if read_json(os.path.join(workspace, "mutants", "mutmut-stats.json")) is not None:
        return True
    returncode, suite = run_tool([python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--ignore=mutants"],
                                 workspace, deadline - time.monotonic(), "pytest", budget=timeout)
    if returncode not in (0, 5):  # 5: pytest collected no test, so nothing can reach the changed code
        print(output + suite, file=sys.stderr)
        raise CannotRun("the project's tests fail without any mutant (pytest exited %d; the output is above)" % returncode)
    note("no test reaches a function the pull request changed, so every mutant on its changed lines is not covered")
    return False


def select(workspace, prefix, ranges):
    """Step 2: {mutant's full name: (path, line, original text, mutated text)} for the mutants on changed lines."""
    mutants, where = os.path.join(workspace, "mutants"), {}
    for path, spans in sorted(ranges.items()):
        with open(os.path.join(workspace, path), encoding="utf-8") as handle:
            source = handle.read()
        left_out(prefix + path, source, spans)
        meta = read_json(os.path.join(mutants, path + ".meta"))
        spans_file = read_json(os.path.join(mutants, path + ".spans"))
        if meta is None or spans_file is None:
            if functions(source)[0]:
                raise CannotRun("mutmut wrote no mutants for %s, which has functions it mutates" % path)
            continue
        with open(os.path.join(mutants, path), encoding="utf-8") as handle:
            placed = place(path, source, handle.read(), spans_file.get("spans", {}))
        for name in meta.get("exit_code_by_key", {}):
            line, before, after = placed[name.rpartition(".")[2]]
            if any(first <= line <= last for first, last in spans):
                where[name] = (path, line, before, after)
    return where


def classify(where, codes, prefix, output):
    """(killed, undetected, excluded) from mutmut's exit code per mutant."""
    killed, undetected, excluded = 0, [], []
    for name in sorted(where, key=lambda n: where[n][:2]):
        path, line, before, after = where[name]
        code, text = codes.get(name), "`%s` -> `%s`" % (clip(before), clip(after))
        if code in KILLED:
            killed += 1
        elif code in UNDETECTED:
            undetected.append(((prefix + path, line), "mutant %s: %s" % (UNDETECTED[code], text)))
        elif code in EXCLUDED:
            excluded.append(((prefix + path, line), "mutant %s, left out of the score: %s" % (EXCLUDED[code], text)))
        else:
            print(output, file=sys.stderr)
            raise CannotRun("mutmut left mutant %s with exit code %r: the run did not finish" % (name, code))
    return killed, undetected, excluded


def mutate(repo, prefix, ranges, python, timeout):
    """Run mutmut on {path relative to the project: [(first, last)]}; (killed, undetected, excluded), the last
    two as [((path, line), text)] with repository paths."""
    deadline = time.monotonic() + timeout
    python = interpreter(python, repo)
    site = run_tool([ensure_python_tool(TOOL, python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                    None, 60, "python")[1].strip().splitlines()[-1]
    with tempfile.TemporaryDirectory(prefix="vv-mutation-py-") as workspace:
        copy_project(repo, prefix, workspace)
        write_config(workspace, sorted(ranges))
        boot = [python, "-c", BOOT % site, "run"]
        covered = collect(boot, python, workspace, deadline, timeout)
        where = select(workspace, prefix, ranges)
        codes, output = {name: 33 for name in where}, ""  # 33: mutmut's "no tests"
        if where and covered:
            _, output = run_tool(boot + sorted(where), workspace, deadline - time.monotonic(), "mutmut", budget=timeout)
            for path in ranges:
                codes.update((read_json(os.path.join(workspace, "mutants", path + ".meta")) or {}).get("exit_code_by_key", {}))
    return classify(where, codes, prefix, output)


def clip(text):
    text = " ".join(str(text).split())
    return text if len(text) <= 60 else text[:57] + "..."
