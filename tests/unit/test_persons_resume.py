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
        self.config = Settings(data_dir=Path(tmp.name))
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

    @patch("pauk.pipeline.stages.persons.OrcidClient")
    @patch("pauk.pipeline.stages.persons.CrossrefClient")
    @patch("pauk.pipeline.stages.persons.OpenAlexClient")
    def test_legacy_openreview_state_does_not_block_orcid_or_resume(self, openalex, _crossref, orcid):
        self.prepared.write_rows("persons", [{
            "id": "P1", "is_itmo": True, "orcid": "0000-0001-2345-6789",
            "openreview": "~Ada_Lovelace1",
            "_processing": {"openreview": {"status": "failed", "phase": "email"}},
        }])
        orcid.return_value.get_record.return_value = {"person": {"researcher-urls": {
            "researcher-url": [
                {"url": {"value": "https://github.com/ada"}},
                {"url": {"value": "https://scholar.google.com/citations?user=ada"}},
            ],
        }}}

        stage = PersonsStage(self.prepared, self.raw, self.config)
        result = stage.run()
        person = next(self.prepared.read_models("persons", Person))
        self.assertEqual(person.github, "ada")
        self.assertEqual(person.google_scholar, "https://scholar.google.com/citations?user=ada")
        self.assertEqual(person.processing["orcid"].status, ProcessingStatus.COMPLETED)
        self.assertNotIn("openreview", person.model_dump())
        self.assertEqual(set(result), {"persons", "crossref"})

        self.assertEqual(stage.run(), {"persons": 0, "crossref": 0})
        orcid.return_value.get_record.assert_called_once_with("0000-0001-2345-6789")
        openalex.return_value.get_author.assert_not_called()


if __name__ == "__main__":
    unittest.main()
