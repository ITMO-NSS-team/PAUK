"""Unit tests for authorship.py."""

from __future__ import annotations

import unittest

from new_generate.authorship import build_authorship_index


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
