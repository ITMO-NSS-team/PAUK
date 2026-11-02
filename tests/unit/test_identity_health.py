import unittest

import mongomock

from pauk.admin.identity_health import collect


class IdentityHealthTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().db

    def checks(self) -> dict[str, dict]:
        return {row["id"]: row for row in collect(self.db)}

    def test_detects_nested_doi_identity_and_reference_failures(self):
        self.db.publications.insert_many([
            {
                "_id": "pub_1", "id": "pub_1",
                "identifiers": [{
                    "scheme": "doi", "value": "10.1/SAME", "normalized_key": "doi:10.1/same",
                }],
                "source_records": [{
                    "key": "catalog:P1", "source": "catalog", "source_id": "P1",
                    "doi": "10.1/same",
                    "authors": [{"person_id": "missing"}],
                }],
            },
            {
                "_id": "pub_2", "id": "pub_2",
                "source_records": [{
                    "key": "archive:A1", "source": "archive", "source_id": "A1",
                    "doi": "https://doi.org/10.1/SAME",
                }],
            },
        ])
        self.db.identity_keys.insert_one({
            "_id": "publication:doi:10.1/same",
            "entity": "publication",
            "scheme": "doi",
            "entity_id": "pub_2",
        })
        self.db.persons.insert_one({
            "_id": "per_1", "id": "per_1",
            "authored": [{"publication_id": "missing"}],
        })
        checks = self.checks()
        self.assertGreater(checks["identity_key_owner"]["n"], 0)
        self.assertEqual(checks["duplicate_normalized_doi"]["n"], 1)
        self.assertEqual(checks["dangling_publication_reference"]["n"], 1)
        self.assertEqual(checks["source_record_owner"]["n"], 2)
        self.assertEqual(checks["dangling_source_author"]["n"], 1)

    def test_clean_identity_graph_passes(self):
        self.db.publications.insert_one({
            "_id": "pub_1", "id": "pub_1",
            "identifiers": [{
                "scheme": "catalog", "value": "P1", "normalized_key": "catalog:P1",
            }],
            "source_records": [{
                "key": "catalog:P1", "source": "catalog", "source_id": "P1",
            }],
        })
        self.db.identity_keys.insert_one({
            "_id": "publication:catalog:P1",
            "entity": "publication",
            "scheme": "catalog",
            "entity_id": "pub_1",
        })
        self.assertTrue(all(row["status"] == "ok" for row in collect(self.db)))


if __name__ == "__main__":
    unittest.main()
