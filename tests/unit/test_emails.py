import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mongomock

from pauk.models import Person, Publication
from pauk.pipeline.stages.emails import (
    EmailsStage,
    author_surnames,
    emails_in_html,
    emails_in_text,
    owner_of,
    pick_email,
)
from pauk.settings import Settings
from pauk.storage import PreparedStore, RawStore


def person(pid, name, *, itmo=True, email=None, variants=(), works=(), homepage=None):
    return Person(
        id=pid, openalex_id=pid, is_itmo=itmo, name_en=name, email=email,
        homepage=homepage, name_variants=list(variants),
        authored=[{"publication_id": work, "position": 1} for work in works],
    )


def publication(pid, text):
    return Publication(id=pid, title="paper", full_text=text)


class EmailsInTextTest(unittest.TestCase):
    def test_a_plain_address(self):
        self.assertEqual(
            emails_in_text("Contact: Dukhanov@itmo.ru for details"), {"dukhanov@itmo.ru"})

    def test_braces_stand_for_several_authors(self):
        self.assertEqual(
            emails_in_text("{lvkarakchieva, pvtrifonov}@itmo.ru"),
            {"lvkarakchieva@itmo.ru", "pvtrifonov@itmo.ru"})

    def test_a_sentence_period_is_not_part_of_the_address(self):
        self.assertEqual(emails_in_text("write to ivan@itmo.ru."), {"ivan@itmo.ru"})

    def test_an_unknown_suffix_is_not_an_address(self):
        # Bare "@" turns up in citations and file names; only known domain
        # endings are read as addresses.
        self.assertEqual(emails_in_text("see fig@3 and table@2.pdf"), set())


class EmailsInHtmlTest(unittest.TestCase):
    """A page hides an address from scrapers but still shows it to a reader."""

    def test_a_mailto_link(self):
        self.assertEqual(emails_in_html('<a href="mailto:Ivan@itmo.ru">write</a>'),
                         {"ivan@itmo.ru"})

    def test_at_and_dot_spelled_out(self):
        self.assertEqual(emails_in_html("dukhanov [at] itmo [dot] ru"), {"dukhanov@itmo.ru"})

    def test_an_html_entity_for_the_at_sign(self):
        self.assertEqual(emails_in_html("petrov&#64;itmo.ru"), {"petrov@itmo.ru"})

    def test_spaces_around_the_at_sign(self):
        self.assertEqual(emails_in_html("sidorov @ itmo.ru"), {"sidorov@itmo.ru"})


class OwnerOfTest(unittest.TestCase):
    AUTHORS = [("A1", {"dukhanov"}), ("A2", {"trifonov"})]

    def test_the_surname_inside_the_local_part_names_the_owner(self):
        self.assertEqual(owner_of("dukhanov@itmo.ru", self.AUTHORS), "A1")
        self.assertEqual(owner_of("pvtrifonov@itmo.ru", self.AUTHORS), "A2")

    def test_an_address_naming_nobody_is_left_alone(self):
        self.assertIsNone(owner_of("info@itmo.ru", self.AUTHORS))

    def test_two_authors_fitting_one_address_is_not_guessed_at(self):
        namesakes = [("A1", {"petrov"}), ("A2", {"petrov"})]
        self.assertIsNone(owner_of("petrov@itmo.ru", namesakes))

    def test_surnames_are_compared_without_accents(self):
        self.assertEqual(owner_of("muller@uni.de", [("A1", {"muller"})]), "A1")


class AuthorSurnamesTest(unittest.TestCase):
    def test_every_spelling_contributes_a_surname(self):
        self.assertEqual(
            author_surnames(person("A1", "Alexey Dukhanov", variants=["A. Duhanov"])),
            {"dukhanov", "duhanov"})

    def test_a_short_last_word_is_an_initial_not_a_surname(self):
        self.assertEqual(author_surnames(person("A1", "Ivan P.")), set())

    def test_a_single_word_name_has_no_surname(self):
        self.assertEqual(author_surnames(person("A1", "Madonna")), set())


class PickEmailTest(unittest.TestCase):
    def test_a_university_address_wins_even_when_longer(self):
        # Length is only the tie-breaker; the domain decides first.
        self.assertEqual(
            pick_email({"iv@gmail.com", "a.p.dukhanov@itmo.ru"}), "a.p.dukhanov@itmo.ru")

    def test_nothing_usable_gives_nothing(self):
        self.assertIsNone(pick_email(set()))


class EmailsStageTest(unittest.TestCase):
    def run_stage(self, people, publications):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = Settings(data_dir=Path(tmp.name))
        db = mongomock.MongoClient()["pauk_test"]
        self.prepared = PreparedStore(db, "sample")
        self.raw = RawStore(db, "sample")
        self.config = config
        self.prepared.write_models("persons", people)
        self.prepared.write_models("publications", publications)
        result = EmailsStage(self.prepared, self.raw, config).run()
        return result, {p.id: p for p in self.prepared.read_models("persons", Person)}

    def test_an_address_in_the_paper_reaches_its_author(self):
        result, people = self.run_stage(
            [person("A1", "Alexey Dukhanov", works=["W1"])],
            [publication("W1", "Alexey Dukhanov, ITMO University, dukhanov@itmo.ru")],
        )
        self.assertEqual(result["emails_filled"], 1)
        self.assertEqual(people["A1"].email, "dukhanov@itmo.ru")

    def test_a_published_address_is_not_replaced(self):
        _result, people = self.run_stage(
            [person("A1", "Alexey Dukhanov", email="from@orcid.org", works=["W1"])],
            [publication("W1", "dukhanov@itmo.ru")],
        )
        self.assertEqual(people["A1"].email, "from@orcid.org")
        # Kept all the same: the matcher recognises an account by any of them.
        self.assertIn("dukhanov@itmo.ru", people["A1"].emails)

    def test_every_address_found_is_kept(self):
        _result, people = self.run_stage(
            [person("A1", "Alexey Dukhanov", works=["W1", "W2"])],
            [publication("W1", "a.dukhanov@itmo.ru"),
             publication("W2", "dukhanov@gmail.com")],
        )
        self.assertEqual(people["A1"].emails, ["a.dukhanov@itmo.ru", "dukhanov@gmail.com"])

    def test_an_external_author_is_left_alone(self):
        result, people = self.run_stage(
            [person("A1", "Alexey Dukhanov", itmo=False, works=["W1"])],
            [publication("W1", "dukhanov@itmo.ru")],
        )
        self.assertEqual(result["emails_filled"], 0)
        self.assertIsNone(people["A1"].email)

    def test_an_address_from_another_paper_is_not_borrowed(self):
        # W2 is not this author's work, so its addresses say nothing.
        result, people = self.run_stage(
            [person("A1", "Alexey Dukhanov", works=["W1"])],
            [publication("W1", "no addresses here"),
             publication("W2", "dukhanov@itmo.ru")],
        )
        self.assertEqual(result["emails_filled"], 0)
        self.assertIsNone(people["A1"].email)

    def test_a_university_address_wins_over_a_personal_one(self):
        _result, people = self.run_stage(
            [person("A1", "Alexey Dukhanov", works=["W1"])],
            [publication("W1", "dukhanov@aol.com and a.dukhanov@itmo.ru")],
        )
        self.assertEqual(people["A1"].email, "a.dukhanov@itmo.ru")

    @patch("pauk.pipeline.stages.emails.HttpClient")
    def test_the_page_an_author_listed_is_read_for_their_address(self, client):
        # The colleague's address sorts first, so only the surname test
        # can pick the right one.
        client.return_value.get_bytes.return_value = (
            b"Staff: Antonov [at] itmo [dot] ru, Dukhanov [at] itmo [dot] ru")
        result, people = self.run_stage(
            [person("A1", "Alexey Dukhanov", homepage="https://itmo.ru/staff/1", works=["W1"])],
            [publication("W1", "no addresses in the text")],
        )
        self.assertEqual((result["pages"], result["emails_filled"]), (1, 1))
        # The page lists the whole group; only the surname settles which
        # address belongs to this person.
        self.assertEqual(people["A1"].email, "dukhanov@itmo.ru")

    @patch("pauk.pipeline.stages.emails.HttpClient")
    def test_an_author_who_already_has_an_address_is_not_fetched_for(self, client):
        _result, _people = self.run_stage(
            [person("A1", "Alexey Dukhanov", email="known@itmo.ru",
                    homepage="https://itmo.ru/staff/1", works=["W1"])],
            [publication("W1", "text")],
        )
        self.assertFalse(client.return_value.get_bytes.called)

    @patch("pauk.pipeline.stages.emails.HttpClient")
    def test_a_page_that_cannot_be_fetched_costs_nothing(self, client):
        client.return_value.get_bytes.side_effect = RuntimeError("timeout")
        result, people = self.run_stage(
            [person("A1", "Alexey Dukhanov", homepage="https://gone.example", works=["W1"])],
            [publication("W1", "text")],
        )
        self.assertEqual(result["emails_filled"], 0)
        self.assertIsNone(people["A1"].email)

    def test_a_paper_without_text_is_marked_and_skipped(self):
        result, _people = self.run_stage(
            [person("A1", "Alexey Dukhanov", works=["W1"])],
            [Publication(id="W1", title="never downloaded")],
        )
        self.assertEqual((result["publications"], result["emails_filled"]), (1, 0))
        rows = {p.id: p for p in self.prepared.read_models("publications", Publication)}
        self.assertEqual(rows["W1"].processing["emails"].status, "completed_empty")

    def test_a_second_run_reads_nothing_again(self):
        people = [person("A1", "Alexey Dukhanov", works=["W1"])]
        publications = [publication("W1", "dukhanov@itmo.ru")]
        self.run_stage(people, publications)
        result = EmailsStage(self.prepared, self.raw, self.config).run()
        self.assertEqual(result["publications"], 0)


if __name__ == "__main__":
    unittest.main()