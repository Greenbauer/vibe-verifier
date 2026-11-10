"""The `eslint` parser of gates/_parsers.py, on a stand-in that prints ESLint's `--format json` report: one entry per
file, an error on each line holding BAD, a parse error (no rule) on each holding BROKEN, a warning on every file.
ESLint's count ratchet is not tested: it cannot run with a real ESLint (docs/features/tool-descriptors.md)."""
import json
import sys
import unittest

from test_descriptors import StandIn, _parsers

ESLINT = r'''#!/usr/bin/env python3
import json, os, sys
files = [a for a in sys.argv[1:] if os.path.isfile(a)]
def message(n, text):
    if "BROKEN" in text:
        return {"ruleId": None, "fatal": True, "severity": 2, "message": "Parsing error: Unexpected token", "line": n}
    return {"ruleId": "no-bad", "severity": 2, "message": "holds BAD", "line": n, "column": 1}
report = [{"filePath": os.path.abspath(f),
           "messages": [message(n, text) for n, text in enumerate(open(f).read().splitlines(), 1) if "BAD" in text or "BROKEN" in text]
           + [{"ruleId": "no-console", "severity": 1, "message": "Unexpected console statement.", "line": 1}]} for f in files]
print(json.dumps(report))
sys.exit(1 if any(m["severity"] == 2 for r in report for m in r["messages"]) else 0)
'''


class Eslint(StandIn):
    def setUp(self):
        super().setUp()
        tool = self.bin + "/vv-standin"
        with open(tool, "w") as script:
            script.write(ESLINT.replace("#!/usr/bin/env python3", "#!" + sys.executable))

    def eslint_repo(self, base, change, **kwargs):
        return self.repo(base, change, parser="eslint", **kwargs)

    def test_eslint_errors_block_and_its_warnings_advise(self):
        result = self.run_gate(self.eslint_repo({}, {"src/a.txt": "fine\nBAD\n"}), "--base-ref", "HEAD~1", "--format", "json")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["findings"], [
            {"message": "no-bad: holds BAD", "path": "src/a.txt", "line": 2, "advisory": False},
            {"message": "no-console: Unexpected console statement.", "path": "src/a.txt", "line": 1, "advisory": True}])

    def test_eslint_warnings_alone_pass(self):
        result = self.run_gate(self.eslint_repo({}, {"a.txt": "fine\n"}), "--base-ref", "HEAD~1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("1 advisory finding(s)", result.stdout)

    def test_a_file_eslint_could_not_parse_blocks(self):
        result = self.run_gate(self.eslint_repo({}, {"a.txt": "BROKEN\n"}), "--base-ref", "HEAD~1", "--format", "json")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["findings"][0],
                         {"message": "parse: Parsing error: Unexpected token", "path": "a.txt", "line": 1, "advisory": False})

    def test_a_message_eslint_gives_no_rule_is_named_for_what_it_is(self):
        report = json.dumps([{"filePath": "a.js", "messages": [
            {"ruleId": None, "fatal": True, "severity": 2, "message": "Parsing error: Unexpected token", "line": 3},
            {"ruleId": None, "severity": 1, "message": "Unused eslint-disable directive.", "line": 1}]}])
        findings = _parsers.eslint(report, {"parser": "eslint"}, ".")
        self.assertEqual([(f.rule, f.advisory) for f in findings], [("parse", False), ("eslint", True)])


if __name__ == "__main__":
    unittest.main()
