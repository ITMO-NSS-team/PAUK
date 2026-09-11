import unittest

import mongomock

from pauk.graph.jsonl_loader import extract_repo_links, load_prepared_rows, normalize_repo_url
from pauk.graph.load import ENTITY_FILES
from pauk.graph.mutations import merge_nodes
from pauk.graph.unmerge import split_person
from pauk.models import Person, Publication
from pauk.storage import PreparedStore
from tests.unit.test_admin_nodes import FakePanelGraph


class NormalizeRepoUrlTest(unittest.TestCase):
    def test_case_and_cosmetic_suffixes_are_ignored(self):
        canonical = normalize_repo_url("https://github.com/Org/Repo")
        for variant in (
            "https://github.com/org/repo",
            "https://github.com/Org/Repo/",
            "https://github.com/Org/Repo.git",
            "https://www.github.com/Org/Repo",
            "https://www.github.com/Org/Repo.GIT",
        ):
            self.assertEqual(normalize_repo_url(variant), canonical)


class ExtractRepoLinksTest(unittest.TestCase):
    def test_known_repository_matched_case_insensitively_by_stored_url(self):
        known = {normalize_repo_url("https://github.com/Org/Repo"): "https://github.com/Org/Repo"}
        row = {"publication_id": "W1", "links": [{
            "url": "https://github.com/org/repo",
            "occurrences": [{"context": "code", "page_number": None}, {"context": "again", "page_number": 3}],
        }]}
        candidates, repo_edges, candidate_edges, promotions = extract_repo_links(row, known)
        self.assertEqual(candidates, [])
        self.assertEqual(candidate_edges, [])
        # The edge targets the URL as stored on the Repository node. Occurrences
        # flatten to parallel arrays; page_number=None (abstract) becomes 0
        # since Neo4j array properties can't hold null.
        self.assertEqual(repo_edges, [(
            "W1", "https://github.com/Org/Repo",
            {"context": ["code", "again"], "page_number": [0, 3]},
        )])
        self.assertEqual(promotions, [("https://github.com/org/repo", "https://github.com/Org/Repo")])

    def test_unknown_url_becomes_link_candidate(self):
        row = {"publication_id": "W1", "links": [{"url": "https://example.org/data", "host": "example.org"}]}
        candidates, repo_edges, candidate_edges, promotions = extract_repo_links(row, {})
        self.assertEqual(candidates, [("https://example.org/data", {"url": "https://example.org/data", "host": "example.org"})])
        self.assertEqual(repo_edges, [])
        self.assertEqual(candidate_edges, [("W1", "https://example.org/data", {})])
        self.assertEqual(promotions, [])

    def test_link_without_url_is_skipped(self):
        row = {"publication_id": "W1", "links": [{"url": None, "context": "broken"}]}
        self.assertEqual(extract_repo_links(row, {}), ([], [], [], []))


if __name__ == "__main__":
    unittest.main()


class MergesSurviveAPublishTest(unittest.TestCase):
    """A fold the rows never learned about must outlive a republish.

    Only the collection stage writes a fold into the prepared rows. The
    graph-wide pass and the review queue write it onto the node and nowhere
    else, and publishing the survivor from its row used to replace that list
    with the row's empty one — after which the alias map resolved nothing
    and the duplicate came back with all of its relationships.
    """

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        self.prepared = PreparedStore(self.db, "sample")
        self.prepared.write_models("persons", [
            Person(id="A1", openalex_id="A1", name_raw="Ivan Smirnov", is_itmo=True,
                   authored=[{"publication_id": "W1", "position": 1}]),
            Person(id="A2", openalex_id="A2", name_raw="I. Smirnov", is_itmo=False,
                   authored=[{"publication_id": "W2", "position": 1}]),
        ])
        self.prepared.write_models("publications", [
            Publication(id="W1", title="W1"), Publication(id="W2", title="W2")])
        self.graph = FakePanelGraph()
        self.publish()

    def publish(self):
        load_prepared_rows(self.graph, {
            filename: list(self.prepared.read_rows(entity))
            for entity, filename in ENTITY_FILES.items()})

    def people(self):
        return sorted(node_id for label, node_id in self.graph.nodes if label == "Person")

    def authored(self):
        return sorted((src_id, tgt_id) for _s, rel, _t, src_id, tgt_id
                      in self.graph.relationships if rel == "AUTHORED")

    def test_the_pair_is_two_records_until_something_folds_it(self):
        self.assertEqual(self.people(), ["A1", "A2"])

    def test_a_fold_made_on_the_graph_outlives_a_publish(self):
        merge_nodes(self.graph, "Person", "A2", "A1")
        self.publish()
        self.assertEqual(self.people(), ["A1"])

    def test_and_the_work_stays_where_the_fold_put_it(self):
        merge_nodes(self.graph, "Person", "A2", "A1")
        self.publish()
        self.assertEqual(self.authored(), [("A1", "W1"), ("A1", "W2")])

    def test_the_survivor_still_answers_for_the_folded_id(self):
        merge_nodes(self.graph, "Person", "A2", "A1")
        self.publish()
        self.assertEqual(self.graph.fetch_merged_id_map("Person"), {"A2": "A1"})

    def test_a_fold_taken_apart_is_not_put_back(self):
        # The other direction, and the one a union gets wrong if it only
        # ever adds: the undo removes the id from the node, and the publish
        # must not restore it from a row that never had it either.
        merge_nodes(self.graph, "Person", "A2", "A1")
        split_person(self.graph, self.db, ["A1", "A2"])
        self.publish()
        self.assertEqual(self.people(), ["A1", "A2"])
        self.assertEqual(self.authored(), [("A1", "W1"), ("A2", "W2")])


class StoppedMidPublishError(Exception):
    """Stands in for the worker's Cancelled, which lives a layer away."""


class PublishProgressTest(unittest.TestCase):
    """A publish says how far it has got, and can be given up between chunks.

    Until now the whole load was one call that came back when it was done.
    A cancel pressed during it was noticed after it finished, which for a
    large group is the one moment nobody was waiting for.
    """

    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        self.prepared = PreparedStore(self.db, "sample")
        self.prepared.write_models("persons", [
            Person(id=f"A{index}", openalex_id=f"A{index}", name_raw=f"Person {index}",
                   is_itmo=True, authored=[{"publication_id": "W1", "position": 1}])
            for index in range(3)])
        self.prepared.write_models("publications", [Publication(id="W1", title="W1")])
        self.graph = FakePanelGraph()

    def publish(self, report=None):
        load_prepared_rows(self.graph, {
            filename: list(self.prepared.read_rows(entity))
            for entity, filename in ENTITY_FILES.items()}, report=report)

    def test_it_says_what_it_is_doing(self):
        told = []
        self.publish(report=lambda *row: told.append(row))
        steps = {step for step, _done, _total in told}
        self.assertEqual(steps, {"выкладка узлов", "выкладка связей"})

    def test_the_counts_add_up_to_everything_there_was(self):
        told = []
        self.publish(report=lambda *row: told.append(row))
        nodes = [row for row in told if row[0] == "выкладка узлов"][-1]
        self.assertEqual(nodes[1], nodes[2])
        links = [row for row in told if row[0] == "выкладка связей"][-1]
        self.assertEqual(links[1], links[2])

    def test_a_publish_can_be_given_up_part_way(self):
        def refuse(step, done, total):
            raise StoppedMidPublishError(step)

        with self.assertRaises(StoppedMidPublishError):
            self.publish(report=refuse)

    def test_and_what_it_wrote_before_that_is_there(self):
        # Nothing is rolled back: the group is loaded in part, and the next
        # publish finishes it because every write is a MERGE.
        def refuse(step, done, total):
            if step == "выкладка связей":
                raise StoppedMidPublishError(step)

        with self.assertRaises(StoppedMidPublishError):
            self.publish(report=refuse)
        self.assertEqual(len([key for key in self.graph.nodes if key[0] == "Person"]), 3)
        self.publish()
        self.assertEqual(len(self.graph.relationships), 3)

    def test_a_publish_nobody_is_watching_still_works(self):
        self.publish()
        self.assertEqual(len([key for key in self.graph.nodes if key[0] == "Person"]), 3)
