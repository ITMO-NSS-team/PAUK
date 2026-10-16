import unittest
from unittest.mock import MagicMock, Mock, patch

from pauk.graph.client import Neo4jClient
from pauk.graph.dedup import dedup_graph_persons
from pauk.graph.person_resolution import Decision, ModelVerdict, PairEvidence, resolve_pair
from pauk.models import Person
from pauk.pipeline.person_name_blocking import candidate_person_pairs
from pauk.pipeline.person_resolution_planner import plan_person_merges_resolved
from pauk.pipeline.stages.dedup import _finalize_person_merge_groups, plan_person_merges


class DedupReviewRegressionTest(unittest.TestCase):
    def test_shared_context_does_not_override_full_name_conflicts(self):
        for names in (
            ("Anton A. Muravev", "Ekaterina A. Muraveva"),
            ("Artem Nazarenko", "Dmitry Nazarenko"),
            ("Roman Polishchuk", "Elena V. Polishchuk"),
            ("Andrey Bogdanov", "Bogdan Bogdanov"),
        ):
            for publications, coauthors in ((1, 0), (0, 1), (3, 5)):
                with self.subTest(names=names, publications=publications, coauthors=coauthors):
                    evidence = PairEvidence(
                        person_a="A", name_a=names[0], person_b="B", name_b=names[1],
                        shared_publications=publications, shared_coauthors=coauthors,
                    )
                    with patch("pauk.graph.person_resolution.logistic_probability", return_value=0.995):
                        resolution = resolve_pair(evidence)
                    self.assertEqual(resolution.decision, Decision.FIRST_MODEL)
                    self.assertEqual(resolution.route, "surname_mismatch")

    def test_hyphenated_given_names_do_not_become_explicit_surnames(self):
        for first, second in (
            ("Jean-Pierre Dupont", "Jean-Pierre Durand"),
            ("Xiao-Long Wang", "Xiao-Long Wu"),
        ):
            with self.subTest(names=(first, second)):
                people = [Person(id="A", name_raw=first, is_itmo=True), Person(id="B", name_raw=second, is_itmo=True)]
                self.assertEqual(list(candidate_person_pairs(people, None)), [])

    def test_connected_ambiguous_bridges_remain_separate(self):
        for field in ("orcid", "email"):
            with self.subTest(field=field):
                people = {identifier: Person(id=identifier, is_itmo=True) for identifier in ("A", "B", "M", "N")}
                setattr(people["A"], field, "one")
                setattr(people["B"], field, "two")
                edges = [("A", "M"), ("M", "B"), ("A", "N"), ("N", "B"), ("M", "N")]
                rules = {frozenset(edge): "qwen_second_merge" for edge in edges}
                orcids = {identifier: person.orcid for identifier, person in people.items()}
                groups, report = _finalize_person_merge_groups(edges, rules, people, orcids)
                self.assertEqual(groups, [])
                self.assertTrue(any(row["status"] == "held" for row in report))

    def test_legacy_manual_answer_cannot_merge_a_multi_person_record(self):
        first = Person(id="A", name_raw="Anna Sidorova and Ivan Petrov", is_itmo=True)
        second = Person(id="B", name_raw="Anna Sidorova", is_itmo=True)
        for candidates in ([], [(first, second)]):
            with self.subTest(blocked=bool(candidates)):
                with patch("pauk.pipeline.stages.dedup._paired_persons", return_value=iter(candidates)):
                    groups, report = plan_person_merges(
                        [first, second], {}, decisions={frozenset(("A", "B")): "same"},
                    )
                self.assertEqual(groups, [])
                self.assertEqual(len(report), 1)
                self.assertEqual(report[0]["status"], "disputed")

    def test_graph_passes_parsed_names_and_aliases_to_the_planner(self):
        fields = {
            "surname_ru": "Иванов", "surname_en": "Ivanov",
            "first_name_ru": "Пётр", "first_name_en": "Petr",
            "other_names": ["P. S. Ivanov"], "name_variants": ["Ivanov P. S."],
        }
        client = Mock()
        client.fetch_persons_for_dedup.return_value = [{"id": "A", "name_raw": "Ivanov Petr Sergeevich", **fields}]
        client.fetch_publication_fields.return_value = {}
        with patch("pauk.graph.dedup.plan_person_merges_resolved", return_value=([], [])) as planner:
            dedup_graph_persons(client, {}, models=Mock())
        person = planner.call_args.args[0][0]
        for name, value in fields.items():
            with self.subTest(field=name):
                self.assertEqual(getattr(person, name), value)

    def test_graph_query_fetches_the_name_fields(self):
        client = Neo4jClient.__new__(Neo4jClient)
        client.driver = MagicMock()
        transaction = Mock()
        transaction.run.return_value = []
        session = client.driver.session.return_value.__enter__.return_value
        session.execute_read.side_effect = lambda read: read(transaction)

        self.assertEqual(client.fetch_persons_for_dedup(), [])

        query = transaction.run.call_args.args[0]
        for field in ("surname_ru", "surname_en", "first_name_ru", "first_name_en", "other_names"):
            with self.subTest(field=field):
                self.assertIn(f"p.{field} AS {field}", query)

    def test_disagreement_keeps_the_guard_route_and_both_verdicts(self):
        people = [
            Person(id="A", name_raw="Andrey V. Lyamin", is_itmo=True),
            Person(id="B", name_raw="Andrey Volchek", is_itmo=True),
        ]
        models = Mock()
        models.first_many.side_effect = lambda rows: {i: ModelVerdict(True, 0.9, "first") for i, _ in rows}
        models.second_many.side_effect = lambda rows: {r.pair_id: ModelVerdict(False, 0.8, "second") for r in rows}
        with (
            patch("pauk.pipeline.stages.dedup._paired_persons", return_value=iter([tuple(people)])),
            patch("pauk.graph.person_resolution.logistic_probability", return_value=0.995),
        ):
            groups, report = plan_person_merges_resolved(people, {}, models=models)

        self.assertEqual(groups, [])
        (held,) = report
        self.assertEqual(held["route"], "qwen_disagreement")
        self.assertEqual(held["input_route"], "surname_mismatch")
        self.assertEqual(held["model_verdicts"]["first"]["reason"], "first")
        self.assertEqual(held["model_verdicts"]["second"]["reason"], "second")
