"""_schema_subset: the JSON Schema subset harnesses/proof-audit/schema.json uses, checked without a dependency."""
import json
import sys
import unittest

from helpers import ROOT

sys.path.insert(0, str(ROOT / "gates"))
from _schema_subset import schema_problem  # noqa: E402

SCHEMA = json.loads((ROOT / "harnesses" / "proof-audit" / "schema.json").read_text())
GRADE = {"id": "C-8", "status": "UNPROVEN", "cases": [], "exclusion": None, "missing": "no test"}


class SchemaSubset(unittest.TestCase):
    def test_a_grade_that_matches_has_no_problem(self):
        self.assertIsNone(schema_problem({"requirements": [GRADE]}, SCHEMA))

    def test_each_rule_names_the_first_place_it_breaks(self):
        for value, problem in (([], "grades is not object"),
                               ({}, "grades lacks requirements"),
                               ({"requirements": [GRADE], "extra": 1}, "grades has extra"),
                               ({"requirements": [dict(GRADE, status="MAYBE")]}, "grades.requirements[0].status is 'MAYBE', not one of"),
                               ({"requirements": [dict(GRADE, cases="none")]}, "grades.requirements[0].cases is not array")):
            with self.subTest(problem=problem):
                self.assertIn(problem, schema_problem(value, SCHEMA) or "")

    def test_an_integer_is_never_a_boolean(self):
        self.assertIsNone(schema_problem(3, {"type": "integer"}))
        self.assertEqual(schema_problem(True, {"type": "integer"}), "grades is not integer")


if __name__ == "__main__":
    unittest.main()
