import unittest

import mongomock

from pauk.models import Department, Person
from scripts.migrate_local_names import migrate_mongo


class MigrateMongoTest(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient()["pauk_test"]
        self.db.persons.insert_one({"_id": "A1", "id": "A1", "is_itmo": True, "name_ru": "Никитин Николай",
                                    "surname_ru": "Никитин", "first_name_ru": "Николай", "second_name_ru": None})
        self.db.departments.insert_one({"_id": "D1", "id": "D1", "name_en": "Faculty", "name_ru": "Факультет"})
        self.db.revisions.insert_one({"entity_type": "persons", "entity_id": "A1", "version": 1,
                                      "snapshot": {"id": "A1", "is_itmo": False, "name_ru": "Никитин"}})
        self.db.person_merge_sources.insert_one({"_id": "h", "person_id": "A1",
                                                 "snapshot": {"id": "A1", "is_itmo": True}})
        self.db.graph_overrides.insert_one({"_id": "Person:A1", "label": "Person",
                                            "fields": {"name_ru": "Никитин Н. О."},
                                            "auto_value": {"name_ru": "Никитин Николай"}})

    def test_a_dry_run_counts_and_changes_nothing(self):
        counts = migrate_mongo(self.db, apply=False)
        self.assertEqual(counts["persons:is_itmo"], 1)
        self.assertEqual(counts["revisions:snapshot.name_ru"], 1)
        self.assertEqual(counts["graph_overrides:fields.name_ru"], 1)
        self.assertIn("is_itmo", self.db.persons.find_one({"_id": "A1"}))

    def test_every_copy_is_renamed_and_still_validates(self):
        migrate_mongo(self.db, apply=True)
        person = self.db.persons.find_one({"_id": "A1"})
        self.assertNotIn("is_itmo", person)
        self.assertEqual(Person.model_validate(person).surname_local, "Никитин")
        self.assertTrue(Person.model_validate(person).is_member)
        self.assertEqual(Department.model_validate(self.db.departments.find_one()).name_local, "Факультет")
        self.assertEqual(self.db.revisions.find_one()["snapshot"], {"id": "A1", "is_member": False,
                                                                    "name_local": "Никитин"})
        self.assertTrue(Person.model_validate(self.db.person_merge_sources.find_one()["snapshot"]).is_member)
        override = self.db.graph_overrides.find_one()
        self.assertEqual(override["fields"], {"name_local": "Никитин Н. О."})
        self.assertEqual(override["auto_value"], {"name_local": "Никитин Николай"})
        self.assertEqual(migrate_mongo(self.db, apply=False), {})

    def test_a_value_already_under_the_new_name_wins(self):
        self.db.persons.update_one({"_id": "A1"}, {"$set": {"name_local": "Никитин Николай Олегович"}})
        migrate_mongo(self.db, apply=True)
        person = self.db.persons.find_one({"_id": "A1"})
        self.assertEqual(person["name_local"], "Никитин Николай Олегович")
        self.assertNotIn("name_ru", person)


if __name__ == "__main__":
    unittest.main()
