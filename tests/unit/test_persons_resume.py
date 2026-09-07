import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mongomock

from pauk.models import Person
from pauk.models.processing import ProcessingStatus
from pauk.pipeline.stages.persons import PersonsStage
from pauk.settings import Settings
from pauk.storage import PreparedStore, RawStore


class PersonsResumeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = Settings(data_dir=Path(tmp.name), openreview_username="", openreview_password="")
        db = mongomock.MongoClient()["pauk_test"]
        self.prepared = PreparedStore(db, "sample")
        self.raw = RawStore(db, "sample")

    @patch("pauk.pipeline.stages.persons.OrcidClient")
    @patch("pauk.pipeline.stages.persons.CrossrefClient")
    @patch("pauk.pipeline.stages.persons.OpenAlexClient")
    def test_completed_source_is_saved_before_later_person_interrupts(self, openalex, _crossref, _orcid):
        self.prepared.write_models("persons", [
            Person(id="P1", openalex_id="A1", is_itmo=False),
            Person(id="P2", openalex_id="A2", is_itmo=False),
        ])
        openalex.return_value.get_author.side_effect = [
            {"display_name": "First"}, KeyboardInterrupt(),
        ]
        with self.assertRaises(KeyboardInterrupt):
            PersonsStage(self.prepared, self.raw, self.config).run()

        rows = {person.id: person for person in self.prepared.read_models("persons", Person)}
        self.assertEqual(rows["P1"].processing["openalex_author"].status, ProcessingStatus.COMPLETED)
        self.assertNotIn("openalex_author", rows["P2"].processing)

    @patch("pauk.pipeline.stages.persons.OpenReviewClient")
    @patch("pauk.pipeline.stages.persons.OrcidClient")
    @patch("pauk.pipeline.stages.persons.CrossrefClient")
    @patch("pauk.pipeline.stages.persons.OpenAlexClient")
    def test_openreview_email_batch_completes_person(self, openalex, _crossref, _orcid, openreview):
        config = Settings(
            data_dir=self.config.data_dir, openreview_username="user", openreview_password="password",
        )
        self.prepared.write_models("persons", [
            Person(id="P1", is_itmo=True, name_raw="Ada Lovelace", email="ada@itmo.ru"),
        ])
        openreview.return_value.search_emails.return_value = {
            "profiles": [{"id": "~Ada_Lovelace1", "email": "ada@itmo.ru", "content": {"github": "ada"}}],
        }
        PersonsStage(self.prepared, self.raw, config).run()

        person = next(self.prepared.read_models("persons", Person))
        state = person.processing["openreview"]
        self.assertEqual(state.status, ProcessingStatus.COMPLETED)
        self.assertEqual(state.phase, "email")
        self.assertEqual(person.github, "ada")
        self.assertEqual(openreview.return_value.search_emails.call_count, 1)


if __name__ == "__main__":
    unittest.main()
