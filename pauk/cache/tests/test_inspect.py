"""Unit tests for inspect.py: JSON-text decoding, table/sample printing."""

from __future__ import annotations

import io
import re
import unittest
from contextlib import redirect_stdout

from pauk.cache.inspect import _decode_json_text, describe_table, sample_rows, summarize


class DecodeJsonTextTest(unittest.TestCase):
    def test_decodes_a_known_json_text_field(self):
        self.assertEqual(
            _decode_json_text("affiliations", '[{"name": "ITMO"}]'), [{"name": "ITMO"}]
        )

    def test_leaves_an_unknown_field_untouched_even_if_json_shaped(self):
        # Decision is made by field name, not content - e.g. a publication
        # title could legitimately start with "[".
        self.assertEqual(
            _decode_json_text("title", '["not", "real", "json-text"]'),
            '["not", "real", "json-text"]',
        )

    def test_leaves_null_untouched(self):
        self.assertIsNone(_decode_json_text("funding", None))

    def test_leaves_broken_json_untouched_instead_of_raising(self):
        self.assertEqual(_decode_json_text("versions", "{not valid json"), "{not valid json")


class DescribeTableTest(unittest.TestCase):
    def test_reports_null_count_per_field(self):
        rows = [
            {"id": "p1", "email": "a@b.c"},
            {"id": "p2", "email": None},
            {"id": "p3", "email": None},
        ]
        buf = io.StringIO()
        with redirect_stdout(buf):
            describe_table(rows, "persons")
        output = buf.getvalue()
        self.assertRegex(output, r"\bid\b")
        self.assertRegex(output, r"email\s+null=\s*2\b")

    def test_empty_table_reports_no_rows_instead_of_raising(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            describe_table([], "persons")
        self.assertIn("persons: no rows", buf.getvalue())

    def test_sorts_fields_by_null_count_descending(self):
        # Sparsest fields first - that's the whole point of this view.
        rows = [
            {"a": None, "b": None, "c": 1},
            {"a": None, "b": 2, "c": 1},
            {"a": None, "b": 2, "c": 1},
        ]
        buf = io.StringIO()
        with redirect_stdout(buf):
            describe_table(rows, "t")
        fields_in_order = [line.split()[0] for line in buf.getvalue().splitlines()]
        self.assertEqual(fields_in_order, ["a", "b", "c"])


class SummarizeTest(unittest.TestCase):
    def test_lists_every_table_with_its_row_count(self):
        graph = {"persons": [{"id": "p1"}], "publications": []}
        buf = io.StringIO()
        with redirect_stdout(buf):
            summarize(graph)
        output = buf.getvalue()
        self.assertRegex(output, r"persons.*\b1\b")
        self.assertRegex(output, r"publications.*\b0\b")


class SampleRowsTest(unittest.TestCase):
    def test_expands_json_text_field_instead_of_printing_escaped_string(self):
        rows = [{"id": "pub1", "funding": '[{"agency": "RSF"}]'}]
        buf = io.StringIO()
        with redirect_stdout(buf):
            sample_rows(rows, 1)
        output = buf.getvalue()
        self.assertIn('"agency": "RSF"', output)
        self.assertNotIn('\\"', output)

    def test_respects_the_sample_limit(self):
        rows = [{"id": f"p{i}"} for i in range(5)]
        buf = io.StringIO()
        with redirect_stdout(buf):
            sample_rows(rows, 2)
        printed_ids = re.findall(r'"id": "(p\d)"', buf.getvalue())
        self.assertEqual(printed_ids, ["p0", "p1"])


if __name__ == "__main__":
    unittest.main()
