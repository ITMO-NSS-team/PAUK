import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import mongomock

from pauk.models import Person
from pauk.models.processing import ProcessingStatus
from pauk.pipeline.stages.persons import PersonsStage
from pauk.settings import Settings
from pauk.sources.orcid import OrcidAuthenticationError
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
            {"id": "A1", "display_name": "First"}, KeyboardInterrupt(),
        ]
        openalex.return_value.get_authors.return_value = {}
        with self.assertRaises(KeyboardInterrupt):
            PersonsStage(self.prepared, self.raw, replace(self.config, persons_concurrency=1)).run()

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

    @patch("pauk.pipeline.stages.persons.OrcidClient")
    @patch("pauk.pipeline.stages.persons.CrossrefClient")
    @patch("pauk.pipeline.stages.persons.OpenAlexClient")
    def test_authentication_error_stops_the_stage_without_losing_openalex(self, openalex, _crossref, orcid):
        self.prepared.write_models("persons", [
            Person(id="P1", openalex_id="A1", orcid="0000-0001-2345-6789", is_itmo=False),
            Person(id="P2", openalex_id="A2", orcid="0000-0001-2345-6790", is_itmo=False),
        ])
        openalex.return_value.get_authors.return_value = {
            "A1": {"id": "A1", "display_name": "Author"}, "A2": {"id": "A2", "display_name": "Author"},
        }
        orcid.return_value.authorize.side_effect = OrcidAuthenticationError("ORCID rejected the credentials")
        config = replace(self.config, orcid_client_id="app", orcid_client_secret="secret")
        with self.assertRaises(OrcidAuthenticationError):
            PersonsStage(self.prepared, self.raw, config).run()
        self.assertEqual(orcid.call_count, 1)
        self.assertEqual(orcid.call_args.args, (config.request_timeout, "app", "secret"))
        self.assertEqual(orcid.call_args.kwargs["requests_per_second"], config.orcid_rps)
        orcid.return_value.get_record.assert_not_called()
        rows = {person.id: person for person in self.prepared.read_models("persons", Person)}
        self.assertEqual(rows["P1"].processing["openalex_author"].status, ProcessingStatus.COMPLETED)
        self.assertNotIn("orcid", rows["P1"].processing)
        self.assertNotIn("orcid", rows["P2"].processing)


if __name__ == "__main__":
    unittest.main()
