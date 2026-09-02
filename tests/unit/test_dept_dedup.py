import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import mongomock

from pauk.graph.dept_dedup.adjudicate import Adjudicator, _parse, build_prompt
from pauk.graph.dept_dedup.matching import (
    AUTO_MERGE,
    AUTO_REJECT,
    LLM,
    DepartmentRecord,
    assign_band,
    block,
    score_pair,
)
from pauk.graph.dept_dedup.normalize import normalize
from pauk.graph.dept_dedup.pipeline import _run
from pauk.settings import Settings


def rec(dept_id, name_en=None, name_ru=None, variants=(), kind=None, parent=None,
        staff=(), pubs=()):
    return DepartmentRecord(
        id=dept_id, name_en=name_en, name_ru=name_ru, name_variants=tuple(variants),
        kind=kind, parent_id=parent, staff_ids=frozenset(staff), publication_ids=frozenset(pubs),
    )


class NormalizeTest(unittest.TestCase):
    def test_folds_spelling_and_morphology_into_one_text(self):
        # same word order: British/US spelling and function words fold away
        self.assertEqual(
            normalize("Center of Chemical Engineering").text,
            normalize("Centre for Chemical Engineering").text,
        )

    def test_word_order_shares_a_token_set_not_a_text(self):
        a = normalize("Center for Chemical Engineering")
        b = normalize("Chemical Engineering Centre")
        self.assertNotEqual(a.text, b.text)   # order preserved for ratio scoring
        self.assertEqual(a.tokens, b.tokens)  # but the token set matches

    def test_domain_excludes_qualifier_tokens(self):
        n = normalize("International Research Center for Optical Materials Science")
        self.assertIn("optical", n.domain)
        self.assertIn("material", n.domain)
        self.assertNotIn("center", n.domain)
        self.assertNotIn("research", n.domain)
        self.assertNotIn("international", n.domain)

    def test_cyrillic_homoglyphs_do_not_split_a_name(self):
        # "А" here is Cyrillic U+0410
        self.assertEqual(normalize("АI Institute").text, normalize("AI Institute").text)

    def test_detects_explicit_acronym_and_trailing_form(self):
        self.assertEqual(normalize("FBIT").acronym, "FBIT")
        self.assertEqual(normalize("Center for Educational Neuroscience (CEdNe)").acronym, "CEDNE")
        self.assertIsNone(normalize("Faculty of Physics").acronym)


class BandingTest(unittest.TestCase):
    def test_typo_pair_auto_merges(self):
        sig = score_pair(rec("a", "Chemical Engineering Center"),
                         rec("b", "Chemical Engineerin Center"))
        self.assertTrue(sig.guard_clear)
        self.assertEqual(assign_band(sig), AUTO_MERGE)

    def test_word_reorder_auto_merges(self):
        sig = score_pair(rec("a", "Center for Educational Neuroscience"),
                         rec("b", "Center for Neuroscience in Education"))
        self.assertEqual(assign_band(sig), AUTO_MERGE)

    def test_one_domain_word_apart_is_not_auto_merge(self):
        sig = score_pair(rec("a", "Center for Artificial Intelligence in Chemistry"),
                         rec("b", "Center for Artificial Intelligence in Agrobiotechnology"))
        self.assertFalse(sig.guard_clear)
        self.assertIn("agrobiot", sig.head_diff + tuple())
        self.assertNotEqual(assign_band(sig), AUTO_MERGE)

    def test_incompatible_kind_never_auto_merges(self):
        sig = score_pair(rec("a", "Faculty of Photonics", kind="faculty"),
                         rec("b", "School of Photonics", kind="megafaculty"))
        self.assertFalse(sig.kinds_compatible)
        self.assertNotEqual(assign_band(sig), AUTO_MERGE)

    def test_unrelated_names_auto_reject(self):
        sig = score_pair(rec("a", "Laboratory of Youth Robotics"),
                         rec("b", "Center for Social and Human Sciences"))
        self.assertEqual(assign_band(sig), AUTO_REJECT)

    def test_semantic_cosine_lifts_a_cross_language_pair_to_llm(self):
        sig = score_pair(rec("a", "Faculty of Physics"), rec("b", "Physics Faculty"),
                         embedding_cosine=0.83)
        self.assertIn(assign_band(sig), {AUTO_MERGE, LLM})


class BlockingTest(unittest.TestCase):
    def test_blocks_typo_pair_and_skips_unrelated(self):
        records = [
            rec("a", "Department of Nanophotonics and Metamaterials"),
            rec("b", "Department of Nanophotonics and Metamatarials"),
            rec("c", "Laboratory of Cultural Heritage Studies"),
        ]
        pairs = block(records)
        self.assertIn(("a", "b"), pairs)
        self.assertNotIn(tuple(sorted(("a", "c"))), pairs)

    def test_semantic_pairs_are_added(self):
        records = [rec("a", "X"), rec("b", "Y")]
        self.assertIn(("a", "b"), block(records, {("a", "b")}))


class FakeOpenRouter:
    def __init__(self, replies):
        self.model = "test/model"
        self._replies = list(replies)
        self.last_response = self.last_usage = self.last_error = None
        self.prompts = []

    def chat_json(self, prompt):
        self.prompts.append(prompt)
        return self._replies.pop(0) if self._replies else None


class AdjudicatorTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]

    def test_verdict_is_cached_and_reused(self):
        fake = FakeOpenRouter([{"relation": "same", "confidence": 0.95, "reason": "rename"}])
        adj = Adjudicator(self.db, fake)
        a = rec("a", "Faculty of Digital Transformations", "Факультет цифровых трансформаций")
        b = rec("b", "AI Technologies Faculty", "Факультет технологий искусственного интеллекта")
        sig = score_pair(a, b)

        first = adj.verdict(a, b, sig)
        second = adj.verdict(a, b, sig)
        self.assertTrue(first.is_merge)
        self.assertEqual(second.relation, "same")
        self.assertEqual(adj.calls, 1)
        self.assertEqual(adj.cache_hits, 1)

    def test_failed_call_is_not_cached(self):
        fake = FakeOpenRouter([None, {"relation": "unrelated", "confidence": 0.9, "reason": "x"}])
        adj = Adjudicator(self.db, fake)
        a, b = rec("a", "A lab"), rec("b", "B lab")
        sig = score_pair(a, b)
        self.assertEqual(adj.verdict(a, b, sig).relation, "unknown")   # not persisted
        self.assertEqual(adj.verdict(a, b, sig).relation, "unrelated")  # retried, then cached
        self.assertEqual(len(fake.prompts), 2)

    def test_parse_rejects_unknown_relation(self):
        self.assertEqual(_parse({"relation": "maybe", "confidence": 1}).relation, "unknown")
        self.assertEqual(_parse(None).relation, "unknown")

    def test_prompt_carries_context(self):
        a = rec("a", "Research Center of Light-Guided Photonics", kind="center", parent="p1", staff=["s1"])
        b = rec("b", "Laboratory of Waveguide Photonics", kind="lab", parent="p1")
        prompt = build_prompt(a, b, score_pair(a, b))
        self.assertIn("Light-Guided Photonics", prompt)
        self.assertIn("same parent:     yes", prompt)


class FakeClient:
    def __init__(self, rows):
        self._rows = rows
        self.upserts = []
        self.merges = []

    def fetch_departments_for_dedup(self):
        return [dict(r) for r in self._rows]

    def upsert_nodes_batch(self, label, batch):
        self.upserts.append((label, list(batch)))

    def merge_department_nodes_batch(self, batch):
        self.merges.extend(batch)
        return len(batch)


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config = Settings(data_dir=Path(self._tmp.name), neo4j_password="x",
                               openrouter_api_key="")

    def _journal(self):
        path = Path(self._tmp.name) / "cache" / "dedup_candidates_departments.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_auto_merge_folds_and_writes_journal(self):
        rows = [
            {"id": "chem-eng", "name_en": "Center for Chemical Engineering", "name_ru": None,
             "name_variants": [], "kind": "center", "parent_id": "itmo", "merged_ids": [],
             "staff_ids": ["s1"], "publication_ids": ["p1"]},
            {"id": "chem-eng-2", "name_en": "Chemical Engineering Centre", "name_ru": None,
             "name_variants": [], "kind": "center", "parent_id": "itmo", "merged_ids": [],
             "staff_ids": [], "publication_ids": []},
            {"id": "youth-robotics", "name_en": "Laboratory of Youth Robotics", "name_ru": None,
             "name_variants": [], "kind": "lab", "parent_id": "itmo", "merged_ids": [],
             "staff_ids": [], "publication_ids": []},
        ]
        client = FakeClient(rows)
        result = _run(client, self.config, self.db, dry_run=False)

        self.assertEqual(result["merges_applied"], 1)
        self.assertEqual(client.merges, [("chem-eng-2", "chem-eng")])
        self.assertEqual(client.upserts[0][0], "Department")
        self.assertIn("Chemical Engineering Centre", client.upserts[0][1][0][1]["name_variants"])
        merged_rows = [r for r in self._journal() if r["status"] == "merged"]
        self.assertEqual(merged_rows[0]["merged_into"], "chem-eng")

    def test_dry_run_computes_but_does_not_apply(self):
        rows = [
            {"id": "a", "name_en": "Chemical Engineering Center", "name_ru": None,
             "name_variants": [], "kind": "center", "parent_id": None, "merged_ids": [],
             "staff_ids": [], "publication_ids": []},
            {"id": "b", "name_en": "Chemical Engineerin Center", "name_ru": None,
             "name_variants": [], "kind": "center", "parent_id": None, "merged_ids": [],
             "staff_ids": [], "publication_ids": []},
        ]
        client = FakeClient(rows)
        result = _run(client, self.config, self.db, dry_run=True)
        self.assertEqual(result["merges_applied"], 0)
        self.assertEqual(client.merges, [])
        self.assertTrue(any(r["status"] == "merged" for r in self._journal()))

    def test_llm_band_held_without_api_key(self):
        rows = [
            {"id": "a", "name_en": "Faculty of Biotechnologies", "name_ru": None,
             "name_variants": [], "kind": "faculty", "parent_id": None, "merged_ids": [],
             "staff_ids": [], "publication_ids": []},
            {"id": "b", "name_en": "Faculty of Food Biotechnologies and Engineering", "name_ru": None,
             "name_variants": [], "kind": "faculty", "parent_id": None, "merged_ids": [],
             "staff_ids": [], "publication_ids": []},
        ]
        client = FakeClient(rows)
        result = _run(client, self.config, self.db, dry_run=False)
        self.assertGreaterEqual(result["llm_pairs"], 1)
        self.assertEqual(result["merges_applied"], 0)

    def test_kind_incompatible_pair_is_never_merged(self):
        rows = [
            {"id": "fac", "name_en": "Faculty of Photonics", "name_ru": None, "name_variants": [],
             "kind": "faculty", "parent_id": None, "merged_ids": [], "staff_ids": [], "publication_ids": []},
            {"id": "mega", "name_en": "School of Photonics", "name_ru": None, "name_variants": [],
             "kind": "megafaculty", "parent_id": None, "merged_ids": [], "staff_ids": [], "publication_ids": []},
        ]
        client = FakeClient(rows)
        result = _run(client, self.config, self.db, dry_run=False)
        self.assertEqual(result["merges_applied"], 0)
        self.assertEqual(result["auto_merges"], 0)


if __name__ == "__main__":
    unittest.main()
