"""The health checks, against the schema they are supposed to be about.

Nothing tested these before, and it showed: twelve of them went on asking
about `:Person:Itmo` long after the loader stopped writing that label, and
answered "0 of 0 — fine" for a year. A check that quietly stops checking is
worse than no check, because it is read as good news.

The queries cannot be run here — that needs a live Neo4j — so what is
guarded is the part that rotted: the names they are written against.
"""

import re
import unittest

from pauk.graph.mutations import NODE_FIELDS, RELATIONSHIPS
from pauk.gui.checks import BY_ID, CHECKS, GROUP_EN

#: Labels and relationship types as Cypher writes them: after a colon,
#: inside a node pattern `(a:Label)` or a relationship one `[r:TYPE]`.
#: Chained labels — `(p:Person:Itmo)` — are why the names are pulled out of
#: the whole bracketed chunk rather than matched one at a time.
INSIDE = re.compile(r"\(([^()]*)\)|\[([^\[\]]*)\]")
NAME = re.compile(r":([A-Z][A-Za-z_]*)")


def names_in(cypher: str) -> set[str]:
    """Every label and relationship type one query mentions."""
    found: set[str] = set()
    for match in INSIDE.finditer(cypher or ""):
        found.update(NAME.findall(match.group(1) or match.group(2) or ""))
    return found


class NamesExistTest(unittest.TestCase):
    """Every label and type a check names has to be one the graph has."""

    def setUp(self):
        self.known = set(NODE_FIELDS) | {rel_type for _src, rel_type, _tgt in RELATIONSHIPS}

    def test_every_query_asks_about_something_real(self):
        for check in CHECKS:
            for part, cypher in (("count", check.count), ("of", check.of),
                                 ("examples", check.examples)):
                with self.subTest(check=check.id, part=part):
                    unknown = names_in(cypher) - self.known
                    self.assertEqual(unknown, set())

    def test_the_guard_would_have_caught_the_label_that_went_away(self):
        # The bug this test exists for: `Itmo` was a label until the loader
        # moved the distinction onto a property, and the checks kept asking
        # for it. Without this line the test above could pass by matching
        # nothing at all.
        self.assertEqual(names_in("MATCH (p:Person:Itmo) RETURN count(p)") - self.known,
                         {"Itmo"})

    def test_and_reads_a_relationship_type_too(self):
        self.assertEqual(names_in("MATCH (a:Person)-[:AUTHORED]->(b:Publication) RETURN a"),
                         {"Person", "AUTHORED", "Publication"})

    def test_a_property_name_is_not_mistaken_for_a_label(self):
        # `{id: eid}` and `p.name_ru` are not names of anything in the
        # schema, and a guard that read them as labels would fail on every
        # honest query.
        self.assertEqual(names_in("MATCH (e:Person {id: eid}) WHERE e.name_ru IS NULL RETURN e"),
                         {"Person"})


class ShapeTest(unittest.TestCase):
    """The parts of a check the page relies on being there."""

    def test_ids_are_unique(self):
        ids = [check.id for check in CHECKS]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_check_is_findable_by_id(self):
        self.assertEqual(set(BY_ID), {check.id for check in CHECKS})

    def test_warning_comes_before_failure(self):
        for check in CHECKS:
            with self.subTest(check=check.id):
                self.assertLessEqual(check.warn, check.fail)

    def test_every_group_has_an_english_name(self):
        # The map's tab is bilingual; a group missing from the table falls
        # back to the Russian and reads as a bug in the English version.
        self.assertEqual({check.group for check in CHECKS} - set(GROUP_EN), set())

    def test_an_examples_query_takes_the_limit_it_is_given(self):
        # collect_examples passes $lim. A query ignoring it would pull the
        # whole graph into a page.
        for check in CHECKS:
            if check.examples:
                with self.subTest(check=check.id):
                    self.assertIn("$lim", check.examples)

    def test_a_share_has_something_to_be_a_share_of(self):
        # Thresholds below 1 read as fractions, and without a denominator
        # they would be compared against a raw count instead.
        for check in CHECKS:
            with self.subTest(check=check.id):
                if check.fail < 1:
                    self.assertIsNotNone(check.of)

    def test_both_titles_are_filled_in(self):
        for check in CHECKS:
            with self.subTest(check=check.id):
                self.assertTrue(check.title.strip())
                self.assertTrue(check.title_en.strip())
