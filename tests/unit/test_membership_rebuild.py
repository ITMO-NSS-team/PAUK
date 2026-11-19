import copy
import unittest
from datetime import UTC, datetime
from unittest.mock import patch

import mongomock

from pauk.models import Authorship, Person
from pauk.pipeline.membership import ITMO_ROR_ID
from pauk.pipeline.membership_rebuild import build_membership_source_index, rebuild_from_global_sources
from pauk.pipeline.normalize import _merge_person


class MembershipRebuildTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().membership_rebuild
        self.people = [Person(id=f"A{i}", is_itmo=True, authored=[Authorship(publication_id="W1", position=i)])
                       for i in (1, 2)]
        self.raw = [{
            "source": "openalex_works", "group": "older_group",
            "fetched_at": datetime(2026, 11, 1, tzinfo=UTC),
            "payload": {"id": "W1", "authorships": [
                {"author": {"id": "A1"}, "institutions": []},
                {"author": {"id": "A2"}, "institutions": []},
            ]},
        }]

    def test_reuses_one_source_index_without_rescanning_or_mutating_it(self):
        index = build_membership_source_index(iter(self.raw), iter([{"id": "W1"}]))
        before = copy.deepcopy(index)
        with patch.object(self.db.raw, "find", side_effect=AssertionError("raw rescanned")), \
                patch.object(self.db.publications, "find", side_effect=AssertionError("publications rescanned")):
            for person in [*self.people, *reversed(self.people)]:
                result = rebuild_from_global_sources(self.db, person, source_index=index)
                self.assertEqual(result.membership_status, "unsupported")
                self.assertFalse(result.is_itmo)
                self.assertTrue(person.is_itmo)
        self.assertEqual(index, before)

    def test_index_and_legacy_raw_rows_entry_points_agree(self):
        index = build_membership_source_index(self.raw, [])
        for person in self.people:
            direct = rebuild_from_global_sources(self.db, person, raw_rows=self.raw)
            indexed = rebuild_from_global_sources(self.db, person, source_index=index)
            self.assertEqual(direct.membership_status, indexed.membership_status)
            self.assertEqual(direct.membership_work_sources, indexed.membership_work_sources)

    def test_missing_source_is_not_a_negative_certificate(self):
        index = build_membership_source_index([], [])
        result = rebuild_from_global_sources(self.db, self.people[0], source_index=index)
        self.assertEqual(result.membership_status, "unknown")
        self.assertTrue(result.is_itmo)

    def test_equal_time_conflicting_work_is_unresolved_in_reused_index(self):
        conflict = copy.deepcopy(self.raw[0])
        conflict["payload"]["authorships"][0]["institutions"] = [{"ror": ITMO_ROR_ID}]
        index = build_membership_source_index([*self.raw, conflict], [])
        self.assertIsNone(index.works["W1"])
        result = rebuild_from_global_sources(self.db, self.people[0], source_index=index)
        self.assertEqual(result.membership_status, "unknown")
        self.assertTrue(result.is_itmo)

    def test_merge_does_not_add_unknown_inventories_to_the_input_source(self):
        extra = self.people[1]
        before = extra.model_copy(deep=True)
        _merge_person(self.people[0], extra)
        self.assertEqual(extra, before)


if __name__ == "__main__":
    unittest.main()
