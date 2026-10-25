from __future__ import annotations

import unittest

from pauk.search import llm_baseline as lb


def _graph(**changes):
    plan = {"type": "graph", "answer_type": "scalar",
            "entities": [{"kind": "department", "name": "Факультет фотоники", "role": None, "param": "department_id"}],
            "rag": None, "rag_param": None,
            "cypher": "MATCH (p:Person)-[:BELONGS_TO]->(:Department {id: $department_id}) RETURN count(p) AS value"}
    return {**plan, **changes}


class _Client:
    """Replies in turn, the last one repeating; counts the calls."""

    model = "m"

    def __init__(self, *replies, error=None):
        self.replies, self.last_error = list(replies), error
        self.last_usage = {"prompt_tokens": 100, "completion_tokens": 20}
        self.system, self.calls, self.max_tokens = None, 0, None

    def chat_json(self, prompt, system_prompt=None, max_tokens=None, reasoning=None):
        self.system, self.max_tokens = system_prompt, max_tokens
        self.calls += 1
        return self.replies[min(self.calls, len(self.replies)) - 1]


class PlanTest(unittest.TestCase):
    def test_the_prompt_examples_are_valid_plans(self):
        for question, raw in lb.EXAMPLES:
            with self.subTest(question=question):
                lb.parse_plan(raw)

    def test_every_type_has_its_parts(self):
        self.assertEqual(lb.parse_plan(_graph()).type, "graph")
        rag = {"type": "rag", "answer_type": "list", "entities": [],
               "rag": {"targets": ["repositories"], "keywords": "speech synthesis"}, "rag_param": None, "cypher": None}
        self.assertEqual(lb.parse_plan(rag).rag.targets, ["repositories"])
        for broken in (_graph(cypher=None),                                   # graph without Cypher
                       {**rag, "rag": None},                                  # rag without search
                       {**rag, "cypher": "MATCH (n) RETURN n"},               # rag with Cypher
                       _graph(type="mixed"),                                  # mixed without rag / rag_param
                       _graph(rag_param="publications")):                     # rag_param outside mixed
            with self.subTest(broken=broken), self.assertRaises(ValueError):
                lb.parse_plan(broken)

    def test_cypher_parameters_are_declared_and_read_only(self):
        with self.assertRaisesRegex(ValueError, "undeclared"):
            lb.parse_plan(_graph(cypher="MATCH (p:Person {id: $person_id}) RETURN p.id"))
        with self.assertRaisesRegex(ValueError, "write clause"):
            lb.parse_plan(_graph(cypher="MATCH (d:Department {id: $department_id}) DETACH DELETE d"))
        mixed = _graph(type="mixed", answer_type="list", rag_param="publications",
                       rag={"targets": ["publications"], "keywords": "photonic crystals"})
        with self.assertRaisesRegex(ValueError, r"does not use \$publications"):
            lb.parse_plan(mixed)
        mixed["cypher"] = ("MATCH (w:Publication)-[:PRODUCED_BY]->(:Department {id: $department_id}) "
                           "WHERE w.id IN $publications RETURN w.id")
        self.assertEqual(lb.parse_plan(mixed).rag_param, "publications")
        with self.assertRaisesRegex(ValueError, "repeat"):
            lb.parse_plan(_graph(entities=[{"kind": "person", "name": "A", "param": "a_id"},
                                           {"kind": "person", "name": "B", "param": "a_id"}],
                                 cypher="MATCH (p:Person {id: $a_id}) RETURN p.id"))

    def test_two_failed_replies_are_error_class_one_not_an_exception(self):
        client = _Client(None, error="HTTP 500")
        result = lb.plan_question(client, "вопрос", year=2026)
        self.assertEqual((result.plan, result.error_class, result.error), (None, lb.INVALID_PLAN, "HTTP 500"))
        self.assertEqual((client.calls, result.attempts), (2, 2))
        result = lb.plan_question(_Client({"type": "graph"}), "вопрос", year=2026)
        self.assertEqual(result.error_class, lb.INVALID_PLAN)
        self.assertEqual(result.raw, [{"type": "graph"}, {"type": "graph"}])  # every attempt kept for the log

    def test_an_unusable_reply_is_asked_once_more(self):
        client = _Client("Извините, не могу разобрать вопрос", _graph())
        result = lb.plan_question(client, "вопрос", year=2026)
        self.assertIsNone(result.error_class)
        self.assertEqual((client.calls, result.attempts), (2, 2))
        self.assertEqual(result.usage["prompt_tokens"], 200)  # both attempts are paid for

    def test_a_good_reply_is_not_repeated_and_states_the_year(self):
        client = _Client(_graph())
        result = lb.plan_question(client, "вопрос", year=2026)
        self.assertIsNone(result.error_class)
        self.assertEqual((client.calls, result.attempts), (1, 1))
        self.assertEqual(result.plan.entities[0].param, "department_id")
        self.assertEqual(result.usage["prompt_tokens"], 100)
        self.assertIn("текущий год 2026", client.system)
        self.assertNotEqual(lb.prompt_id(2026), lb.prompt_id(2027))
        self.assertEqual(client.max_tokens, lb.MAX_TOKENS)  # a looping reply is cut short, not waited for


if __name__ == "__main__":
    unittest.main()
