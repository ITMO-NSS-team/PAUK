from __future__ import annotations

import unittest

from pauk.search import t2c_metrics as m


def _row(plan_type="graph", answer=None, error_class=None, entities=(), targets=("publications",)):
    plan = None if plan_type is None else {"type": plan_type, "rag": {"targets": list(targets), "keywords": "x"}}
    return {"plan": plan, "answer": answer, "error_class": error_class, "entities": list(entities)}


class CompareTest(unittest.TestCase):
    def test_scalars_are_exact_and_within_ten_percent(self):
        gold = {"type": "scalar", "value": 100}
        self.assertEqual(m.compare({"type": "scalar", "value": 100}, gold), {"em": 1.0, "em_tol": 1.0, "f1": 1.0})
        self.assertEqual(m.compare({"type": "scalar", "value": 108}, gold), {"em": 0.0, "em_tol": 1.0, "f1": 1.0})
        self.assertEqual(m.compare({"type": "scalar", "value": 111}, gold)["em_tol"], 0.0)
        self.assertEqual(m.compare({"type": "scalar", "value": 4.85}, {"type": "scalar", "value": 4.85})["em"], 1.0)
        self.assertEqual(m.compare(None, gold), {"em": 0.0, "em_tol": 0.0, "f1": 0.0})

    def test_lists_are_sets_of_ids(self):
        gold = {"type": "list", "value": ["a", "b", "c", "d"]}
        self.assertEqual(m.compare({"type": "list", "value": ["d", "c", "b", "a"]}, gold)["em"], 1.0)
        self.assertAlmostEqual(m.compare({"type": "list", "value": ["a", "b", "x"]}, gold)["f1"], 4 / 7)
        self.assertEqual(m.compare({"type": "scalar", "value": 4}, gold)["f1"], 0.0)  # another answer type

    def test_tables_match_rows_in_any_column_order_with_tolerant_numbers(self):
        gold = {"type": "table", "columns": ["department", "count"], "rows": [["d1", 100], ["d2", 10]]}
        swapped = {"type": "table", "columns": ["n", "dept"], "rows": [[10, "d2"], [105, "d1"]]}
        scores = m.compare(swapped, gold)
        self.assertEqual((scores["f1"], scores["em_tol"], scores["em"]), (1.0, 1.0, 0.0))  # 105 is within 10% only
        self.assertAlmostEqual(m.compare({"type": "table", "rows": [["d1", 100], ["d3", 1]]}, gold)["f1"], 0.5)
        self.assertEqual(m.table_f1([], []), 1.0)

    def test_empty_answers(self):
        self.assertTrue(m.is_empty({"type": "scalar", "value": 0}))
        self.assertTrue(m.is_empty({"type": "table", "columns": [], "rows": []}))
        self.assertFalse(m.is_empty({"type": "list", "value": ["a"]}))
        self.assertFalse(m.is_empty(None))  # no answer at all is a failure, not an empty answer


class RagPartTest(unittest.TestCase):
    def test_strict_needs_grade_two_recall_and_little_grade_zero(self):
        ranked = [f"w{i}" for i in range(10)]
        grades = {f"publications:w{i}": 2 for i in range(9)} | {"publications:w9": 0, "publications:x": 2}
        entry = {"rankings": {m.BEST_VARIANT: {"publications": ranked}}}
        part = m.rag_part(entry, grades, ["publications"])
        kind = part["kinds"]["publications"]
        self.assertAlmostEqual(kind["R2"], 9 / 10)          # 9 of the 10 grade-2 items
        self.assertAlmostEqual(kind["zero_share"], 1 / 10)  # of the 10 returned, not of k
        self.assertTrue(part["strict"])
        grades["publications:w8"] = 0
        self.assertFalse(m.rag_part(entry, grades, ["publications"])["strict"])

    def test_every_target_kind_must_pass_and_a_missing_kind_scores_zero(self):
        entry = {"rankings": {m.BEST_VARIANT: {"publications": ["w1"], "persons": []}}}
        grades = {"publications:w1": 2, "persons:p1": 2}
        part = m.rag_part(entry, grades, ["persons", "publications"])
        self.assertTrue(part["kinds"]["publications"]["strict"])
        self.assertFalse(part["strict"])                    # persons found nothing
        self.assertEqual(m.rag_part(None, grades, ["publications"]), {"nDCG": 0.0, "F1": 0.0, "strict": False,
                                                                       "kinds": {}})

    def test_abstention_follows_the_models_threshold(self):
        low = {"rankings": {m.BEST_VARIANT: {"top_dense": {"Publication": 0.5}}}}
        high = {"rankings": {m.BEST_VARIANT: {"top_dense": {"Publication": 0.95}}}}
        self.assertTrue(m.abstains(low, ["persons", "publications"]))
        self.assertFalse(m.abstains(high, ["persons", "publications"]))


class ErrorTest(unittest.TestCase):
    def test_the_first_failure_in_the_pipeline_order(self):
        meta = {"id": "q1t2", "targets": "persons,publications"}
        gold = {"entities": [{"id": "lab-1"}]}
        ok = _row("mixed", entities=[{"id": "lab-1"}], targets=("persons", "publications"))
        self.assertIsNone(m.error_class(ok, meta, gold))
        self.assertEqual(m.error_class(_row("graph", entities=[{"id": "lab-1"}]), meta, gold), 2)
        self.assertEqual(m.error_class(_row("mixed", entities=[{"id": "lab-2"}], targets=("persons", "publications")),
                                       meta, gold), 3)
        self.assertEqual(m.error_class(_row("mixed", entities=[{"id": "lab-1"}], targets=("publications",)),
                                       meta, gold), 4)
        crashed = _row("mixed", entities=[{"id": "lab-2"}], error_class=5, targets=("persons", "publications"))
        self.assertEqual(m.error_class(crashed, meta, gold), 3)  # the wrong entity came first
        self.assertEqual(m.error_class(_row(None, error_class=1), meta, gold), 1)

    def test_a_broken_no_answer_question_scores_zero(self):
        meta = {"id": "q1t1", "type": "graph", "answer_type": "scalar", "targets": ""}
        gold = {"type": "no_answer", "entities": [], "answer": {"type": "scalar", "value": None}}
        empty = {**_row("graph", answer={"type": "scalar", "value": 0}), "error_class": None}
        broken = {**_row("graph", answer=None, error_class=5)}
        self.assertEqual(m.score_question(empty, meta, gold, {}, {})["score_luna"], 1.0)
        self.assertEqual(m.score_question(broken, meta, gold, {}, {})["score_luna"], 0.0)

    def test_no_answer_is_the_golds_for_graph_and_mixed_and_the_banks_for_rag(self):
        no_answer, answered = {"type": "no_answer"}, {"type": "graph"}
        self.assertEqual(m.question_type({"id": "q1t1", "type": "graph"}, no_answer), "no_answer")
        self.assertEqual(m.question_type({"id": "q1t2", "type": "mixed"}, no_answer), "no_answer")
        self.assertEqual(m.question_type({"id": "q1t1", "type": "no_answer"}, answered), "graph")
        self.assertEqual(m.question_type({"id": "q1t3", "type": "no_answer"}, None), "no_answer")
        self.assertEqual(m.question_type({"id": "q1t3", "type": "topic"}, None), "rag")


class SummaryTest(unittest.TestCase):
    def test_only_rag_is_given_per_judge(self):
        def row(kind, luna, qwen, strict=1.0, error=None):
            return {"type": kind, "score_luna": luna, "score_qwen50": qwen, "strict_luna": strict,
                    "strict_qwen50": strict, "error_class": error, "tokens": 100, "seconds": 2.0}

        rows = [row("graph", 1.0, 1.0), row("graph", 0.0, 0.0, 0.0, 3), row("rag", 0.6, 0.4, 0.0),
                row("mixed", 0.8, 0.8), row("no_answer", 1.0, 1.0)]
        summary = m.summarize(rows)
        graph, rag = summary["types"]["graph"], summary["types"]["rag"]
        self.assertEqual((graph["score"], graph["strict_pass_rate"]), (0.5, 0.5))
        self.assertEqual(graph["error_shares"]["3"], 0.5)
        self.assertEqual(rag["score"], {"luna": 0.6, "qwen50": 0.4})
        self.assertEqual(summary["types"]["mixed"]["judge"], "luna")
        # (0.5 + 0.8 + rag + 1.0) / 4 per judge
        self.assertEqual(summary["macro_score"], {"luna": round((0.5 + 0.8 + 0.6 + 1.0) / 4, 3),
                                                  "qwen50": round((0.5 + 0.8 + 0.4 + 1.0) / 4, 3)})


    def test_the_error_table_counts_every_type_and_all_questions(self):
        rows = [{"type": "graph", "error_class": 3}, {"type": "graph", "error_class": None},
                {"type": "rag", "error_class": 2}, {"type": "rag", "error_class": 2}]
        table = {line["type"]: line for line in m.error_table(rows)}
        self.assertEqual(set(table), {"graph", "rag", "all"})  # types without questions are left out
        self.assertEqual((table["graph"]["counts"]["3"], table["graph"]["counts"]["none"]), (1, 1))
        self.assertEqual(table["rag"]["shares"]["2"], 1.0)
        self.assertEqual(table["all"]["questions"], 4)
        self.assertEqual(table["all"]["shares"]["2"], 0.5)
        self.assertEqual(sum(table["all"]["counts"].values()), 4)  # every question in exactly one column


if __name__ == "__main__":
    unittest.main()
