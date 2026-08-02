import json
import tempfile
import unittest
from pathlib import Path

from pauk.graph.jsonl_loader import load_jsonl_dir
from pauk.models import Person
from pauk.pipeline.stages.dedup import CANDIDATES_FILENAME, DedupStage
from pauk.storage import PreparedStore, RawStore
from tests.bench.mocks import RecordingNeo4jClient


def person(pid, name, works, *, itmo=True, orcid=None, variants=(), merged=()):
    return Person(
        id=pid, openalex_id=pid, is_itmo=itmo, name_en=name, orcid=orcid,
        name_variants=list(variants), merged_ids=list(merged),
        authored=[{"publication_id": w, "position": 1} for w in works],
    )


class DedupStageTest(unittest.TestCase):
    def run_stage(self, people):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.prepared = PreparedStore(root / "prepared", "sample")
        self.raw = RawStore(root / "raw", "sample")
        self.prepared.write_models("persons", people)
        result = DedupStage(self.prepared, self.raw).run()
        return result, {p.id: p for p in self.prepared.read_models("persons", Person)}

    def candidates(self):
        path = self.prepared.group_dir / CANDIDATES_FILENAME
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_same_orcid_merges_even_without_name_or_itmo_evidence(self):
        result, people = self.run_stage([
            person("A1", "Maria Petrova", ["W1", "W2"], itmo=False, orcid="0000-0001"),
            person("A2", "Maria Sidorova", ["W3"], itmo=False, orcid="0000-0001"),
        ])
        self.assertEqual(result["dedup_merged"], 1)
        self.assertEqual(set(people), {"A1"})
        merged = people["A1"]
        self.assertEqual(merged.merged_ids, ["A2"])
        self.assertEqual({a.publication_id for a in merged.authored}, {"W1", "W2", "W3"})
        self.assertIn("Maria Sidorova", merged.name_variants)
        self.assertEqual(merged.processing["dedup"].result_count, 1)

    def test_name_variant_with_shared_coauthor_merges(self):
        result, people = self.run_stage([
            person("A1", "Nikolay O. Nikitin", ["W1", "W2"],
                   variants=["Nikolay Nikitin", "N.O. Nikitin"], orcid="0000-0002"),
            person("A2", "Nikolay Nikitin", ["W3"]),
            person("A3", "Ilia Revin", ["W1", "W3"]),
        ])
        self.assertEqual(result["dedup_merged"], 1)
        self.assertEqual(set(people), {"A1", "A3"})
        self.assertEqual(people["A1"].merged_ids, ["A2"])
        self.assertEqual(self.candidates(), [])

    def test_variant_match_without_shared_coauthor_is_only_a_candidate(self):
        result, people = self.run_stage([
            person("A1", "Nikolay O. Nikitin", ["W1"], variants=["Nikolay Nikitin"]),
            person("A2", "Nikolay Nikitin", ["W3"]),
        ])
        self.assertEqual(result, {"dedup_merged": 0, "dedup_candidates": 1})
        self.assertEqual(set(people), {"A1", "A2"})
        (candidate,) = self.candidates()
        self.assertEqual({candidate["person_a"], candidate["person_b"]}, {"A1", "A2"})
        self.assertEqual(candidate["held_because"], ["no shared coauthors"])

    def test_same_display_name_without_variant_evidence_is_only_a_candidate(self):
        result, people = self.run_stage([
            person("A1", "Ivan Petrov", ["W1"]),
            person("A2", "Ivan Petrov", ["W2"]),
            person("A3", "Shared Coauthor", ["W1", "W2"]),
        ])
        self.assertEqual(result["dedup_merged"], 0)
        self.assertEqual(result["dedup_candidates"], 1)
        (candidate,) = self.candidates()
        self.assertIn("same display name is not confirmed by a name variant",
                      candidate["held_because"])

    def test_conflicting_orcids_block_merge_and_candidates(self):
        result, people = self.run_stage([
            person("A1", "Nikolay O. Nikitin", ["W1"], variants=["Nikolay Nikitin"], orcid="0000-0002"),
            person("A2", "Nikolay Nikitin", ["W3"], orcid="0000-0003"),
            person("A3", "Ilia Revin", ["W1", "W3"]),
        ])
        self.assertEqual(result, {"dedup_merged": 0, "dedup_candidates": 0})
        self.assertEqual(set(people), {"A1", "A2", "A3"})

    def test_external_pair_is_not_merged_and_not_reported(self):
        result, people = self.run_stage([
            person("A1", "Wei Wang", ["W1"], itmo=False, variants=["W. Wang"]),
            person("A2", "W. Wang", ["W2"], itmo=False),
            person("A3", "Shared Coauthor", ["W1", "W2"], itmo=False),
        ])
        self.assertEqual(result, {"dedup_merged": 0, "dedup_candidates": 0})
        self.assertEqual(len(people), 3)

    def test_transitive_orcid_group_folds_into_most_published(self):
        result, people = self.run_stage([
            person("A1", "Name One", ["W1"], orcid="0000-0004"),
            person("A2", "Name Two", ["W2", "W3"], orcid="0000-0004"),
            person("A3", "Name Three", ["W4"], orcid="0000-0004"),
        ])
        self.assertEqual(result["dedup_merged"], 2)
        self.assertEqual(set(people), {"A2"})
        self.assertEqual(sorted(people["A2"].merged_ids), ["A1", "A3"])
        self.assertEqual({a.publication_id for a in people["A2"].authored},
                         {"W1", "W2", "W3", "W4"})

    def test_raw_openalex_orcid_overrides_poisoned_prepared_orcid(self):
        # The crossref backfill can stamp a namesake's ORCID onto the wrong
        # person; the raw OpenAlex author record is the trusted source.
        result, people = self.run_stage([
            person("A1", "Li Li", ["W1"], itmo=False, orcid="0000-0001"),
            person("A2", "Li Li", ["W2"], itmo=False, orcid="0000-0001"),
        ])
        # Without raw records prepared orcids are trusted — they merge...
        self.assertEqual(result["dedup_merged"], 1)
        # ...but with raw author records showing different ORCIDs they must not.
        self.tmp2 = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp2.cleanup)
        root = Path(self.tmp2.name)
        prepared = PreparedStore(root / "prepared", "sample")
        raw = RawStore(root / "raw", "sample")
        prepared.write_models("persons", [
            person("A1", "Li Li", ["W1"], itmo=False, orcid="0000-0001"),
            person("A2", "Li Li", ["W2"], itmo=False, orcid="0000-0001"),
        ])
        raw.append("openalex_authors",
                   {"id": "https://openalex.org/A1", "orcid": "https://orcid.org/0000-0001"},
                   {"author_id": "A1"})
        raw.append("openalex_authors",
                   {"id": "https://openalex.org/A2", "orcid": "https://orcid.org/0000-0002"},
                   {"author_id": "A2"})
        result = DedupStage(prepared, raw).run()
        self.assertEqual(result["dedup_merged"], 0)
        self.assertEqual(len(list(prepared.read_models("persons", Person))), 2)

    def test_transitive_bridge_between_different_orcids_blocks_the_group(self):
        # A and B each legitimately pair with the no-ORCID bridge M, but the
        # resulting group would span two distinct ORCIDs — refuse it whole.
        result, people = self.run_stage([
            person("A1", "Anna Ivanova", ["W1", "W2"], orcid="0000-0001",
                   variants=["A. Ivanova"]),
            person("A2", "A. Ivanova", ["W3"], variants=["Anna Ivanova", "Anna B. Ivanova"]),
            person("A3", "Anna B. Ivanova", ["W4"], orcid="0000-0002"),
            person("A4", "Shared Coauthor", ["W1", "W3", "W4"]),
        ])
        self.assertEqual(result["dedup_merged"], 0)
        self.assertEqual(len(people), 4)

    def test_second_run_is_a_no_op(self):
        _, people = self.run_stage([
            person("A1", "Maria Petrova", ["W1", "W2"], orcid="0000-0001"),
            person("A2", "Maria Sidorova", ["W3"], orcid="0000-0001"),
        ])
        again = DedupStage(self.prepared, self.raw).run()
        self.assertEqual(again["dedup_merged"], 0)
        rows = {p.id: p for p in self.prepared.read_models("persons", Person)}
        self.assertEqual(rows["A1"].merged_ids, ["A2"])


class LoaderPersonMergeTest(unittest.TestCase):
    def test_previously_published_duplicate_node_is_folded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepared = PreparedStore(root / "prepared", "sample")
            client = RecordingNeo4jClient()
            # First publish: the split still exists.
            prepared.write_models("publications", [])
            prepared.write_models("persons", [
                person("A1", "Nikolay O. Nikitin", ["W1"]),
                person("A2", "Nikolay Nikitin", ["W2"]),
            ])
            client.upsert_nodes_batch("Publication", [("W1", {}), ("W2", {})])
            load_jsonl_dir(client, prepared.group_dir)
            self.assertIn("A2", client.nodes["Person"])
            # Second publish after dedup: A2 folded into A1.
            prepared.write_models("persons", [
                person("A1", "Nikolay O. Nikitin", ["W1", "W2"], merged=["A2"]),
            ])
            load_jsonl_dir(client, prepared.group_dir)
            self.assertNotIn("A2", client.nodes["Person"])
            self.assertEqual(
                {pair for pair in client.edge_pairs("AUTHORED") if pair[0] in {"A1", "A2"}},
                {("A1", "W1"), ("A1", "W2")},
            )


if __name__ == "__main__":
    unittest.main()
