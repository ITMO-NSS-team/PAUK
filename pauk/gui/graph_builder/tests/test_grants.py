"""Unit tests for grants.py - spellings taken from real OpenAlex funding data."""

from __future__ import annotations

import unittest

from pauk.gui.graph_builder.grants import add_grant_keys, normalize_grant_id


class NormalizeGrantIdTest(unittest.TestCase):
    def test_spellings_of_one_grant_share_a_key(self):
        spellings = [
            "075-15-2021-1349",
            "agreement No: 075-15-2021-1349",
            "NO. 075-15-2021-1349",
            "075–15–2021–1349",  # en dashes
            "075-15-2021-1349?",
            "#075-15-2021-1349",
        ]
        self.assertEqual({normalize_grant_id(s) for s in spellings}, {"075-15-2021-1349"})

    def test_program_names_are_kept_as_grants(self):
        self.assertEqual(normalize_grant_id('Priority 2030"'), "PRIORITY2030")
        self.assertEqual(normalize_grant_id("Horizon 2020"), "HORIZON2020")

    def test_cut_short_and_too_short_numbers_have_no_key(self):
        self.assertIsNone(normalize_grant_id("18-19-"))
        self.assertIsNone(normalize_grant_id("12"))
        self.assertIsNone(normalize_grant_id(""))


def _pub(key: str, *grant_ids: str) -> dict:
    return {"key": key, "funding": [{"funder": "Russian Science Foundation", "grant_id": g} for g in grant_ids]}


class AddGrantKeysTest(unittest.TestCase):
    def test_prefix_of_several_grants_is_dropped_not_glued(self):
        pubs = [_pub("P1", "075-15"), _pub("P2", "075-15-2021-1349"), _pub("P3", "075-15-2019-1896")]
        add_grant_keys(pubs)
        self.assertEqual(
            [pub["funding"][0]["grant_key"] for pub in pubs], [None, "075-15-2021-1349", "075-15-2019-1896"]
        )

    def test_prefix_of_exactly_one_grant_joins_it(self):
        pubs = [_pub("P1", "EP-V013025"), _pub("P2", "EP-V013025-1")]
        add_grant_keys(pubs)
        self.assertEqual({pub["funding"][0]["grant_key"] for pub in pubs}, {"EP-V013025-1"})

    def test_chain_of_prefixes_resolves_to_the_longest(self):
        pubs = [_pub("P1", "075-15"), _pub("P2", "075-15-2021"), _pub("P3", "075-15-2021-1349")]
        add_grant_keys(pubs)
        self.assertEqual({pub["funding"][0]["grant_key"] for pub in pubs}, {"075-15-2021-1349"})

    def test_entries_keep_their_raw_fields(self):
        pubs = [_pub("P1", "Grant 18-19-00627")]
        add_grant_keys(pubs)
        self.assertEqual(
            pubs[0]["funding"],
            [{"funder": "Russian Science Foundation", "grant_id": "Grant 18-19-00627", "grant_key": "18-19-00627"}],
        )


class DedupeWithinPublicationTest(unittest.TestCase):
    def test_repeat_fragment_and_bare_funder_of_a_listed_grant_are_dropped(self):
        ministry = "Ministry of Science and Higher Education of the Russian Federation"
        pub = {
            "key": "P1",
            "funding": [
                {"funder": ministry, "grant_id": "075-15-2021-1349"},
                {"funder": ministry, "grant_id": "075-15-"},
                {"funder": ministry, "grant_id": "Agreement 075-15-2021-1349"},
                {"funder": ministry, "grant_id": None},
                {"funder": "Russian Foundation for Basic Research", "grant_id": None},
                {"funder": "Russian Foundation for Basic Research", "grant_id": "12"},
            ],
        }
        add_grant_keys([pub])
        self.assertEqual(
            [(e["funder"], e["grant_id"], e["grant_key"]) for e in pub["funding"]],
            [
                (ministry, "075-15-2021-1349", "075-15-2021-1349"),
                # Different funder without a numbered entry, and an unreadable number - both kept.
                ("Russian Foundation for Basic Research", None, None),
                ("Russian Foundation for Basic Research", "12", None),
            ],
        )

    def test_fragment_without_its_full_number_in_the_same_paper_stays(self):
        pub = _pub("P1", "075-15-")
        add_grant_keys([pub])
        self.assertEqual(len(pub["funding"]), 1)


if __name__ == "__main__":
    unittest.main()
