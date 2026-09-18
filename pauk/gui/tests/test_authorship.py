"""Unit tests for authorship.py."""

from __future__ import annotations

import unittest

from pauk.gui.authorship import build_authorship_index


class BuildAuthorshipIndexTest(unittest.TestCase):
    def test_filters_publications_without_any_itmo_author(self):
        db = {
            "publications": [{"id": "P1"}, {"id": "P2"}],
            "authorship": [{"pid": "P1", "per": "A1"}],
        }
        result = build_authorship_index(db)
        self.assertEqual(result.pub_ids, {"P1"})
        self.assertEqual(result.pub_authors, {"P1": ["A1"]})
        self.assertEqual(result.author_pubs, {"A1": ["P1"]})
        self.assertEqual([r["id"] for r in result.pubs_rows], ["P1"])

    def test_external_coauthors_are_indexed_but_do_not_keep_a_publication_alone(self):
        db = {
            "persons": [{"id": "A1", "is_itmo": True}, {"id": "E1", "is_itmo": False}, {"id": "E2", "is_itmo": False}],
            "publications": [{"id": "P1"}, {"id": "P2"}],
            "authorship": [
                {"pid": "P1", "per": "A1"},
                {"pid": "P1", "per": "E1"},
                {"pid": "P2", "per": "E1"},
                {"pid": "P2", "per": "E2"},
            ],
        }
        result = build_authorship_index(db)
        self.assertEqual(result.pub_ids, {"P1"})
        self.assertEqual(result.pub_authors, {"P1": ["A1", "E1"]})
        self.assertEqual(result.author_pubs, {"A1": ["P1"], "E1": ["P1"]})
        self.assertEqual(result.external_ids, {"E1", "E2"})

    def test_person_without_is_itmo_field_counts_as_itmo(self):
        """Snapshots exported before external authors were added have no
        is_itmo field at all - everyone in them is ITMO."""
        db = {
            "persons": [{"id": "A1"}],
            "publications": [{"id": "P1"}],
            "authorship": [{"pid": "P1", "per": "A1"}],
        }
        result = build_authorship_index(db)
        self.assertEqual(result.pub_ids, {"P1"})
        self.assertEqual(result.external_ids, frozenset())
