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
from pathlib import Path

from pauk.cache import export
from pauk.graph.mutations import NODE_FIELDS, RELATIONSHIPS
from pauk.gui.checks import BY_ID, CHECKS, GROUP_EN
from pauk.gui.generate_stats import QUERIES

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


class ExportQueriesTest(unittest.TestCase):
    """The snapshot export reads the same graph and rots the same way.

    Its queries went through the label migration still asking for
    `:Person:Itmo`, and nothing noticed until a full run wrote a map with
    no authors, no authorship and no departments on it. The checks were
    guarded, the export was not.
    """

    def setUp(self):
        self.known = set(NODE_FIELDS) | {rel_type for _src, rel_type, _tgt in RELATIONSHIPS}
        self.source = Path(export.__file__).read_text(encoding="utf-8")

    def queries(self) -> list[str]:
        return [found for found in re.findall(r'"([^"]*MATCH[^"]*)"', self.source)]

    def test_every_export_query_asks_about_something_real(self):
        found = self.queries()
        self.assertGreater(len(found), 5, "the export queries stopped being found")
        for cypher in found:
            with self.subTest(query=cypher[:40]):
                self.assertEqual(names_in(cypher) - self.known, set())


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

    def test_the_counts_beside_the_checks_ask_about_something_real_too(self):
        # The same rot reached the tiles on the map's tab: "ITMO staff: 0"
        # sat there for as long as the checks did, and the top-departments
        # list was simply empty.
        for cypher in QUERIES:
            with self.subTest(query=cypher[:40]):
                self.assertEqual(names_in(cypher) - self.known, set())

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


class PrecedenceTest(unittest.TestCase):
    """`AND` binds tighter than `OR`, and that has already bitten once.

    Every check about staff is scoped with `p.is_itmo AND ...`. Where the
    condition it scopes contains an `OR`, the scope applies to the first
    half alone — `(is_itmo AND missing) OR empty` counts external authors
    too — and the check goes on looking right while answering a different
    question.
    """

    def test_an_or_under_a_scope_is_bracketed(self):
        for check in CHECKS:
            for part, cypher in (("count", check.count), ("of", check.of),
                                 ("examples", check.examples)):
                if not cypher or "is_itmo AND" not in cypher:
                    continue
                scoped = cypher.split("is_itmo AND", 1)[1]
                # Only the condition the scope introduces matters; an OR in
                # a later clause of the query is its own business.
                condition = scoped.split("RETURN")[0].split("OPTIONAL MATCH")[0]
                with self.subTest(check=check.id, part=part):
                    if " OR " in condition:
                        self.assertIn("(", condition.split(" OR ")[0])


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
