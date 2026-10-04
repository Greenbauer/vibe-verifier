#!/usr/bin/env python3
"""Mutation score of a test module against one source file, with no dependencies.

Each mutant changes one operator or constant in TARGET (a comparison, `and`/`or`, `not`, `+`/`-`,
an int or bool constant, a returned value). The mutant is written into a scratch worktree of HEAD,
TESTS runs there, and a mutant the tests still pass is a survivor. Tests run as subprocesses, so
gates the tests invoke by path see the mutant too.

usage: mutation_score.py --target gates/x.py --tests 'test_x.py' [--max-mutants 80] [--format text|json]
"""
import argparse
import ast
import copy
import json
import os
import pathlib
import re
import subprocess
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
SWAP = {
    ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.GtE, ast.GtE: ast.Lt, ast.Gt: ast.LtE,
    ast.LtE: ast.Gt, ast.In: ast.NotIn, ast.NotIn: ast.In, ast.Is: ast.IsNot, ast.IsNot: ast.Is,
    ast.And: ast.Or, ast.Or: ast.And, ast.Add: ast.Sub, ast.Sub: ast.Add,
}


def mutation_sites(tree):
    """(node index in ast.walk order, line, description) for every mutable node."""
    for index, node in enumerate(ast.walk(tree)):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Compare) and type(node.ops[0]) in SWAP:
            op = type(node.ops[0])
            yield index, line, f"{op.__name__} -> {SWAP[op].__name__}"
        elif isinstance(node, (ast.BoolOp, ast.BinOp)) and type(node.op) in SWAP:
            yield index, line, f"{type(node.op).__name__} -> {SWAP[type(node.op)].__name__}"
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            yield index, line, "drop not"
        elif isinstance(node, ast.Constant) and type(node.value) in (int, bool):
            yield index, line, f"{node.value!r} -> {mutated_constant(node.value)!r}"
        elif isinstance(node, ast.Return) and node.value is not None and not (
                isinstance(node.value, ast.Constant) and node.value.value is None):
            yield index, line, "return None"


def mutated_constant(value):
    return (not value) if isinstance(value, bool) else value + 1


class Mutate(ast.NodeTransformer):
    def __init__(self, target_index):
        self.target_index, self.index = target_index, -1
        self.order = {}

    def apply(self, tree):
        self.order = {id(node): i for i, node in enumerate(ast.walk(tree))}
        return self.visit(tree)

    def generic_visit(self, node):
        if self.order.get(id(node)) != self.target_index:
            return super().generic_visit(node)
        if isinstance(node, ast.Compare):
            node.ops = [SWAP[type(node.ops[0])]()] + node.ops[1:]
        elif isinstance(node, (ast.BoolOp, ast.BinOp)):
            node.op = SWAP[type(node.op)]()
        elif isinstance(node, ast.UnaryOp):
            return node.operand
        elif isinstance(node, ast.Constant):
            node.value = mutated_constant(node.value)
        elif isinstance(node, ast.Return):
            node.value = ast.Constant(value=None)
        return node


def sample(sites, limit):
    if len(sites) <= limit:
        return sites
    step = len(sites) / limit
    return [sites[int(i * step)] for i in range(limit)]


def run_tests(tree_dir, pattern):
    env = {**os.environ, "LC_ALL": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        done = subprocess.run(["python3", "-m", "unittest", "discover", "-s", "tests", "-p", pattern],
                              cwd=tree_dir, capture_output=True, text=True, env=env, timeout=300)
    except subprocess.TimeoutExpired:
        return "cannot-run"
    if not re.search(r"Ran [1-9][0-9]* tests?\b", done.stderr):
        return "cannot-run"
    return "survived" if done.returncode == 0 else "killed"


def score(target, pattern, limit):
    relative = pathlib.Path(target)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("target must be a repository-relative tracked file")
    results = []
    with tempfile.TemporaryDirectory() as scratch:
        work = f"{scratch}/tree"
        subprocess.run(["git", "-C", str(ROOT), "worktree", "add", "--detach", "-q", work, "HEAD"], check=True)
        try:
            target_path = pathlib.Path(work) / relative
            tracked = subprocess.run(["git", "-C", work, "ls-files", "-z", "--", str(relative)],
                                     capture_output=True, text=True, check=True).stdout.split("\0")
            if str(relative) not in tracked or not target_path.resolve().is_relative_to(pathlib.Path(work).resolve()):
                raise ValueError("target must be a tracked file inside the scratch worktree")
            # Source and tests must come from the same immutable snapshot.
            source = target_path.read_text()
            tree = ast.parse(source)
            sites = sample(list(mutation_sites(tree)), limit)
            if run_tests(work, pattern) != "survived":
                raise SystemExit(f"{pattern} did not pass a nonempty baseline; no score is possible")
            for index, line, description in sites:
                mutant = ast.unparse(ast.fix_missing_locations(Mutate(index).apply(copy.deepcopy(tree))))
                target_path.write_text(mutant)
                results.append({"line": line, "mutation": description, "result": run_tests(work, pattern)})
            target_path.write_text(source)
        finally:
            subprocess.run(["git", "-C", str(ROOT), "worktree", "remove", "--force", work], check=True)
    killed = sum(r["result"] == "killed" for r in results)
    measured = sum(r["result"] != "cannot-run" for r in results)
    return {"target": target, "tests": pattern, "mutants": len(results), "killed": killed,
            "score": round(killed / measured, 3) if measured else None,
            "cannot_run": [r for r in results if r["result"] == "cannot-run"],
            "survivors": [r for r in results if r["result"] == "survived"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", required=True)
    parser.add_argument("--tests", required=True)
    parser.add_argument("--max-mutants", type=int, default=80)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args()
    if args.max_mutants < 1:
        parser.error("--max-mutants must be positive")
    report = score(args.target, args.tests, args.max_mutants)
    if args.format == "json":
        print(json.dumps(report, indent=2))
        return
    print(f"{report['target']} vs {report['tests']}: {report['killed']}/{report['mutants']} killed, score {report['score']}")
    print(f"  unmeasured: {len(report['cannot_run'])}")
    for survivor in report["survivors"]:
        print(f"  survived  line {survivor['line']:<4} {survivor['mutation']}")


if __name__ == "__main__":
    main()
