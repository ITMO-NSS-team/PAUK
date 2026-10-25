from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from neo4j.exceptions import ClientError, CypherSyntaxError, ServiceUnavailable

from pauk.search import t2c_run


def _plan_record(qid, plan):
    return {"id": qid, "model": "m", "prompt_id": "p", "plan": plan, "error_class": None, "error": None,
            "attempts": 1, "usage": {}, "seconds": 0.1}


class _Index:
    def __init__(self, ids):
        self.ids = ids

    def resolve(self, name):
        found = self.ids.get(name)
        best = SimpleNamespace(id=found, label=name, score=101.0) if found else None
        return SimpleNamespace(best=best, ambiguous=False)


class _Cypher:
    calls: list = []

    def __init__(self, config, timeout):
        pass

    def run(self, text, params):
        _Cypher.calls.append((text, params))
        return [{"id": "A1"}, {"id": "A2"}], None, None

    def close(self):
        pass


class RunTest(unittest.TestCase):
    def test_rows_take_the_shape_of_the_answer(self):
        rows = [{"department": "d1", "count": 3}, {"department": "d2", "count": 1}]
        self.assertEqual(t2c_run.normalize([{"value": 34}], "scalar"), {"type": "scalar", "value": 34})
        self.assertEqual(t2c_run.normalize([], "scalar"), {"type": "scalar", "value": None})
        self.assertEqual(t2c_run.normalize([{"id": "a"}, {"id": "b"}], "list"), {"type": "list", "value": ["a", "b"]})
        self.assertEqual(t2c_run.normalize(rows, "table"),
                         {"type": "table", "columns": ["department", "count"], "rows": [["d1", 3], ["d2", 1]]})
        self.assertEqual(t2c_run.normalize([], "table"), {"type": "table", "columns": [], "rows": []})

    def test_mixed_input_is_what_any_judge_of_the_group_graded_at_least_one(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "a.json").write_text(json.dumps({"q1t3": {"publications:w1": 2, "publications:w2": 0,
                                                         "persons:p1": 1}}), encoding="utf-8")
        (tmp / "b.json").write_text(json.dumps({"q1t3": {"publications:w2": 1, "publications:w3": None}}),
                                    encoding="utf-8")
        with patch.object(t2c_run, "JUDGMENTS", {"3": {"a": tmp / "a.json", "b": tmp / "b.json"}}):
            self.assertEqual(t2c_run.judged_items("q1t3", "publications"), ["w1", "w2"])
            self.assertEqual(t2c_run.judged_items("q1t3", "persons"), ["p1"])
            self.assertEqual(t2c_run.judged_items("q5t1", "publications"), [])  # a group without judgments

    def test_cypher_failures_are_sorted_into_classes(self):
        cypher = t2c_run.Cypher.__new__(t2c_run.Cypher)
        cypher.timeout = 1

        def failing(exc):
            session = SimpleNamespace(execute_read=lambda work: (_ for _ in ()).throw(exc),
                                      __enter__=None, __exit__=None)

            class _Session:
                def __enter__(self):
                    return session

                def __exit__(self, *args):
                    return False

            return SimpleNamespace(session=lambda **kwargs: _Session())

        class _Coded(ClientError):  # the driver's `code` is read-only
            def __init__(self, code):
                super().__init__()
                self._code = code

            @property
            def code(self):
                return self._code

        timed_out = _Coded("Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration")
        missing = _Coded("Neo.ClientError.Statement.ParameterMissing")
        for exc, expected in ((CypherSyntaxError(), t2c_run.CYPHER_REJECTED), (missing, t2c_run.CYPHER_REJECTED),
                              (timed_out, t2c_run.NEO4J_FAILED), (ServiceUnavailable(), t2c_run.NEO4J_FAILED)):
            with self.subTest(exc=type(exc).__name__):
                cypher._driver = failing(exc)
                rows, error_class, _ = cypher.run("MATCH (n) RETURN n", {})
                self.assertEqual((rows, error_class), (None, expected))
        self.assertEqual(cypher.run("MATCH (n) DETACH DELETE n", {})[1], t2c_run.CYPHER_REJECTED)

    def test_answers_resolve_entities_and_feed_mixed_with_judged_items(self):
        questions = [{"id": "q1t1", "question": "сколько"}, {"id": "q2t2", "question": "кто"},
                     {"id": "q3t3", "question": "кто по теме"}, {"id": "q4t1", "question": "кто из Х"},
                     {"id": "q5t1", "question": "сломано"}]
        dept = {"kind": "department", "name": "Лаборатория", "role": None, "param": "department_id"}
        plans = {
            "q1t1": _plan_record("q1t1", {"type": "graph", "answer_type": "list", "entities": [dept], "rag": None,
                                          "rag_param": None, "cypher": "MATCH (p) WHERE p.d = $department_id RETURN p.id"}),
            "q2t2": _plan_record("q2t2", {"type": "mixed", "answer_type": "list", "entities": [dept],
                                          "rag": {"targets": ["publications"], "keywords": "x"},
                                          "rag_param": "publications",
                                          "cypher": "MATCH (w) WHERE w.id IN $publications AND w.d = $department_id "
                                                    "RETURN w.id"}),
            "q3t3": _plan_record("q3t3", {"type": "rag", "answer_type": "list", "entities": [],
                                          "rag": {"targets": ["persons"], "keywords": "x"}, "rag_param": None,
                                          "cypher": None}),
            "q4t1": _plan_record("q4t1", {"type": "graph", "answer_type": "list",
                                          "entities": [{**dept, "name": "Неизвестное"}], "rag": None,
                                          "rag_param": None, "cypher": "MATCH (p) WHERE p.d = $department_id RETURN p"}),
            "q5t1": {**_plan_record("q5t1", None), "error_class": 1, "error": "no response"},
        }
        indexes = {"department": _Index({"Лаборатория": "lab-1"}), "person": _Index({})}
        _Cypher.calls = []
        config = SimpleNamespace(search_dir=Path("."))
        with patch.object(t2c_run, "load_department_index", lambda d: indexes["department"]), \
             patch.object(t2c_run, "load_person_index", lambda d: indexes["person"]), \
             patch.object(t2c_run, "Cypher", _Cypher), \
             patch.object(t2c_run, "judged_items", lambda qid, kind: ["w1", "w2"]):
            rows = {row["id"]: row for row in t2c_run.answer_all(questions, plans, config, timeout=5)}
        self.assertEqual(rows["q1t1"]["params"], {"department_id": "lab-1"})
        self.assertEqual(rows["q1t1"]["answer"], {"type": "list", "value": ["A1", "A2"]})
        self.assertEqual(rows["q2t2"]["params"], {"department_id": "lab-1", "publications": ["w1", "w2"]})
        self.assertIsNone(rows["q3t3"]["answer"])  # answered by the pool
        self.assertEqual(rows["q4t1"]["error_class"], t2c_run.ENTITY_NOT_FOUND)
        self.assertEqual(rows["q5t1"]["error_class"], t2c_run.INVALID_PLAN)
        self.assertEqual(len(_Cypher.calls), 2)  # neither the unresolved nor the failed plan reached Neo4j

    def test_plans_come_from_the_llm_unless_this_run_already_has_them(self):
        good = {"type": "graph", "answer_type": "scalar", "entities": [], "rag": None, "rag_param": None,
                "cypher": "MATCH (p:Person) RETURN count(p) AS value"}

        class _Client:
            model = "m"
            last_error = None
            last_usage = {"prompt_tokens": 10, "completion_tokens": 1}

            def __init__(self):
                self.asked = []

            def chat_json(self, prompt, system_prompt=None, max_tokens=None, reasoning=None):
                self.asked.append(prompt)
                return None if prompt == "broken" else good

        questions = [{"id": "q1t1", "question": "first"}, {"id": "q2t1", "question": "broken"}]
        runs = Path(tempfile.mkdtemp())
        client = _Client()
        with patch.object(t2c_run, "make_client", lambda config: client):
            t2c_run.make_plans(runs / "a", questions, SimpleNamespace(), 2026)
            self.assertEqual(client.asked, ["first", "broken", "broken"])  # all asked; the broken one twice
            client.asked.clear()
            t2c_run.make_plans(runs / "a", questions, SimpleNamespace(), 2026)
            self.assertEqual(client.asked, ["broken", "broken"])  # the same run keeps its good plan only
            client.asked.clear()
            t2c_run.make_plans(runs / "b", questions, SimpleNamespace(), 2026)
            self.assertEqual(client.asked, ["first", "broken", "broken"])  # a new run reuses nothing
            client.asked.clear()
            t2c_run.make_plans(runs / "a", questions, SimpleNamespace(), 2026, replan=True)
            self.assertEqual(client.asked, ["first", "broken", "broken"])  # --replan asks again
            client.asked.clear()
            t2c_run.make_plans(runs / "a", questions, SimpleNamespace(), 2027)
            self.assertEqual(client.asked, ["first", "broken", "broken"])  # another prompt: nothing reused


if __name__ == "__main__":
    unittest.main()
