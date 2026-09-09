"""Юнит-тесты для `inspect.py`: раскрытие JSON-текстовых полей и печать статистики."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout

from new_cache.inspect import _decode_json_text, describe_table, sample_rows, summarize


class DecodeJsonTextTest(unittest.TestCase):
    def test_decodes_known_json_text_field(self):
        self.assertEqual(_decode_json_text("affiliations", '[{"name": "ITMO"}]'), [{"name": "ITMO"}])

    def test_leaves_unknown_field_untouched_even_if_json_shaped(self):
        """Поле не в JSON_TEXT_FIELDS не трогается, даже если содержимое
        случайно похоже на JSON — решение принимается по имени поля, не по
        содержимому (например, название публикации может начинаться с "[")."""
        self.assertEqual(_decode_json_text("title", '["not", "real", "json-text"]'), '["not", "real", "json-text"]')

    def test_leaves_null_untouched(self):
        self.assertIsNone(_decode_json_text("funding", None))

    def test_leaves_broken_json_untouched_instead_of_raising(self):
        self.assertEqual(_decode_json_text("versions", "{not valid json"), "{not valid json")


class DescribeTableTest(unittest.TestCase):
    def test_counts_null_and_types_per_field(self):
        rows = [{"id": "p1", "email": "a@b.c"}, {"id": "p2", "email": None}, {"id": "p3", "email": None}]
        buf = io.StringIO()
        with redirect_stdout(buf):
            describe_table(rows, "persons")
        output = buf.getvalue()
        self.assertIn("id", output)
        self.assertIn("email", output)
        self.assertIn("null=     2", output)

    def test_empty_table_reports_no_rows_instead_of_raising(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            describe_table([], "persons")
        self.assertIn("нет строк", buf.getvalue())


class SummarizeTest(unittest.TestCase):
    def test_lists_every_table_with_row_count_and_fields(self):
        graph = {"persons": [{"id": "p1"}], "publications": []}
        buf = io.StringIO()
        with redirect_stdout(buf):
            summarize(graph)
        output = buf.getvalue()
        self.assertIn("persons: 1 строк", output)
        self.assertIn("publications: 0 строк", output)


class SampleRowsTest(unittest.TestCase):
    def test_expands_json_text_field_in_output(self):
        rows = [{"id": "pub1", "funding": '[{"agency": "RSF"}]'}]
        buf = io.StringIO()
        with redirect_stdout(buf):
            sample_rows(rows, 1)
        output = buf.getvalue()
        # Раскрытое поле выглядит как настоящий вложенный объект в
        # pretty-printed JSON (отступ), а не как экранированная строка (\").
        self.assertIn('"agency": "RSF"', output)
        self.assertNotIn('\\"', output)

    def test_respects_sample_limit(self):
        rows = [{"id": f"p{i}"} for i in range(5)]
        buf = io.StringIO()
        with redirect_stdout(buf):
            sample_rows(rows, 2)
        self.assertEqual(buf.getvalue().count("---"), 2)


if __name__ == "__main__":
    unittest.main()
