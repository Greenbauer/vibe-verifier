# Python test shapes for tests/test_proof_audit_ast_truth.py, which never runs this file: Python's parser says which
# test each assertion line below sits in, and the proof-audit gate must agree. Add a shape here when the gate misreads
# one; never write down what the gate should answer.
import unittest


def helper_before_any_test(value):
    assert value is not None


def test_module_level(record=None):
    assert record is None, "a module-level test"
    assert len(
        [],
    ) == 0


def check_after_a_module_test(value):
    assert value, "a helper after a module-level test is not that test"
    assert isinstance(value, str)


async def test_awaited():
    value = await other()
    assert value == {
        "named": [],
        "count": 0,
    }


class WrappedValues(unittest.TestCase):
    def test_short_values_on_their_own_lines(self):
        self.assertEqual(
            count_rows(),
            0,
        )
        self.assertEqual(
            list_rows(),
            [],
        )
        self.assertIs(
            find_row(),
            None)
        self.assertEqual(
            read_text(),
            "",
        )
        self.assertDictEqual(
            read_map(),
            {},
        )
        self.assertEqual(
            sum_rows(), 1)

    def test_a_comment_and_a_string_at_column_zero_do_not_end_it(self):
        text = """
first line of a fixture
second line
"""
# a comment at column zero
        self.assertIn("second line", text)  # a trailing comment
        self.assertTrue(text.startswith("\nfirst"))

    @unittest.skipIf(False, "a decorated test")
    def test_decorated(
            self,
            *args):
        self.assertFalse(args, "a test whose arguments wrap")
        self.assertEqual(len(args), 0)

    def check_rows(self, rows):
        self.assertEqual(rows, ["a helper method right after a test"])
        self.assertTrue(rows)

    def test_with_a_nested_helper(self):
        def nested(value):
            self.assertTrue(value)
        nested(True)
        self.assertGreater(len(str(nested)), 0)


class HooksAfterTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(True, "a hook after the last class's helper")

    def test_first(self):
        self.assertEqual(sorted([2, 1]), [1, 2])

    def tearDown(self):
        self.assertIsNotNone(self, "a hook right after a test")

    def test_second(self):
        self.assertNotEqual(
            "left",
            "right",
            "a message",
        )
