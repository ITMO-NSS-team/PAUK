"""Two collection groups published into one graph (no network, no DB).

A prepared group is one collection run; the graph accumulates all of them.
That is where a whole class of problems only becomes visible: the same
researcher, work or repository collected in two periods produces two nodes
that no per-group dedup pass can compare, and an author who lost their ITMO
affiliation in one period must not lose the label they earned in another.

The fixture runs the conveyor twice — a 2026 group and a 2025 group — into
one graph double, then applies the graph-wide dedup (`pauk dedup graph`) and
republishes both groups on top of the result.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pauk.graph.dedup import (
    collect_raw_orcids,
    dedup_graph_persons,
    dedup_graph_publications,
    dedup_graph_repositories,
)
from pauk.graph.jsonl_loader import load_jsonl_dir
from pauk.pipeline.collect import Collector
from pauk.pipeline.enrich import Enricher
from pauk.pipeline.normalize import OpenAlexNormalizer
from pauk.pipeline.selectors import PeriodSelector, WorksFileSelector
from pauk.storage import PreparedStore, RawStore
from tests.bench.harness import (
    bench_settings,
    external_services,
    works_file,
    write_static_catalog,
)
from tests.bench.mocks import MockOpenAlexClient, RecordingNeo4jClient
from tests.bench.universe import (
    INCREMENTAL_WORK_IDS,
    PUBLICATION_MERGES,
    build_universe,
)
from tests.bench.universe_2025 import (
    CROSS_PERIOD_PREPRINT_ID,
    CROSS_PERIOD_VOR_ID,
    GAMMA_REPO_ID,
    NIKITIN_2025_ID,
    NIKITIN_2025_NAME,
    NIKITIN_2026_ID,
    OLD_GAMMA_REPO_ID,
    OLD_GAMMA_URL,
    SHARED_WORK_ID,
    STICKY_PERSON_ID,
    WORK_IDS_2025,
    build_universe_2025,
)

GROUP_2026 = "bench-2026"
GROUP_2025 = "bench-2025"


def run_group(config, group: str, universe: dict, selectors) -> PreparedStore:
    """Collect, normalize and enrich one group, then leave it on disk."""
    raw = RawStore(config.raw_dir, group)
    prepared = PreparedStore(config.prepared_dir, group)
    client = MockOpenAlexClient(universe)
    for selector in selectors:
        Collector(client, raw).collect(selector)
    OpenAlexNormalizer(raw, prepared).run()
    with external_services(universe):
        Enricher(prepared, raw, config).run()
    return prepared


@pytest.fixture(scope="module")
def multigroup(tmp_path_factory) -> SimpleNamespace:
    data_dir = tmp_path_factory.mktemp("multigroup-data")
    universe = build_universe()
    universe_2025 = build_universe_2025(universe)
    write_static_catalog(data_dir, universe["departments_catalog"])
    config = bench_settings(data_dir)

    prepared_2026 = run_group(config, GROUP_2026, universe, (
        PeriodSelector("2026-01-01", "2026-12-31"),
        WorksFileSelector(works_file(data_dir, "works-2026.txt", INCREMENTAL_WORK_IDS)),
    ))
    prepared_2025 = run_group(config, GROUP_2025, universe_2025, (
        WorksFileSelector(works_file(data_dir, "works-2025.txt", WORK_IDS_2025)),
    ))

    graph = RecordingNeo4jClient()
    load_jsonl_dir(graph, prepared_2026.group_dir)
    after_2026 = SimpleNamespace(
        publications=set(graph.nodes["Publication"]),
        person_labels=dict(graph.person_labels),
    )
    load_jsonl_dir(graph, prepared_2025.group_dir)
    after_both = SimpleNamespace(
        publications=set(graph.nodes["Publication"]),
        persons=set(graph.nodes["Person"]),
        repositories=set(graph.nodes["Repository"]),
        person_labels=dict(graph.person_labels),
        authored=graph.edge_pairs("AUTHORED"),
    )

    # `pauk dedup graph`, without the Neo4j connection its CLI entry point
    # would open.
    persons_removed, person_report = dedup_graph_persons(
        graph, collect_raw_orcids(config.raw_dir))
    publications_removed, publication_report = dedup_graph_publications(graph)
    repositories_removed, repository_report = dedup_graph_repositories(graph)
    after_dedup = graph.snapshot()

    # Both groups still hold rows for the ids the graph pass folded away.
    load_jsonl_dir(graph, prepared_2025.group_dir)
    after_republish_2025 = graph.snapshot()
    load_jsonl_dir(graph, prepared_2026.group_dir)
    load_jsonl_dir(graph, prepared_2025.group_dir)
    after_republish_both = graph.snapshot()

    return SimpleNamespace(
        config=config,
        graph=graph,
        after_2026=after_2026,
        after_both=after_both,
        after_dedup=after_dedup,
        after_republish_2025=after_republish_2025,
        after_republish_both=after_republish_both,
        removed=SimpleNamespace(persons=persons_removed, publications=publications_removed,
                                repositories=repositories_removed),
        report=[*person_report, *publication_report, *repository_report],
    )


# --- accumulating groups ---------------------------------------------------------

def test_a_work_collected_twice_is_one_publication(multigroup):
    # The 2025 group re-collected W7000000005 verbatim: MERGE by id means one
    # node, and the 2026 group's publications plus the four works only the
    # 2025 group has.
    assert SHARED_WORK_ID in multigroup.after_2026.publications
    assert len(multigroup.after_both.publications) == \
        len(multigroup.after_2026.publications) + len(WORK_IDS_2025) - 1


def test_itmo_label_is_sticky_across_groups(multigroup):
    # External in 2026, ITMO in 2025: at least one ITMO affiliation anywhere
    # makes the person ITMO.
    assert multigroup.after_2026.person_labels[STICKY_PERSON_ID] == "External"
    assert multigroup.after_both.person_labels[STICKY_PERSON_ID] == "Itmo"
    # ...and republishing the group that calls them external cannot undo it.
    _nodes, labels, _edges = multigroup.after_republish_both
    assert labels[STICKY_PERSON_ID] == "Itmo"


def test_authorships_of_both_groups_reach_one_person(multigroup):
    authored = {publication_id for person_id, publication_id
                in multigroup.after_both.authored if person_id == STICKY_PERSON_ID}
    assert "W80000002001" in authored, "the 2025 work is missing"
    assert any(pid.startswith("W7") for pid in authored), "the 2026 works are missing"


# --- graph-wide dedup ------------------------------------------------------------

def test_a_person_split_across_periods_is_invisible_to_the_group_stage(multigroup):
    # Each group's dedup stage only ever saw one of the two records.
    assert {NIKITIN_2026_ID, NIKITIN_2025_ID} <= multigroup.after_both.persons


def test_the_graph_pass_folds_the_person_split_across_periods(multigroup):
    nodes, labels, edges = multigroup.after_dedup
    assert NIKITIN_2025_ID not in nodes["Person"]
    canonical = nodes["Person"][NIKITIN_2026_ID]
    assert canonical["merged_ids"] == [NIKITIN_2025_ID]
    assert NIKITIN_2025_NAME in canonical["name_variants"], "the folded name is lost"
    assert labels[NIKITIN_2026_ID] == "Itmo"
    authored = {tgt for (_src, rel, _tgt_label, src, tgt) in edges
                if rel == "AUTHORED" and src == NIKITIN_2026_ID}
    assert authored == {"W70000000140", "W80000002002"}
    assert multigroup.removed.persons == 1


def test_the_graph_pass_folds_a_preprint_into_a_later_version_of_record(multigroup):
    nodes, _labels, edges = multigroup.after_dedup
    assert CROSS_PERIOD_PREPRINT_ID not in nodes["Publication"]
    survivor = nodes["Publication"][CROSS_PERIOD_VOR_ID]
    assert survivor["merged_ids"] == [CROSS_PERIOD_PREPRINT_ID]
    # The preprint's authors now point at the surviving record.
    authors = {src for (_src_label, rel, _tgt_label, src, tgt) in edges
               if rel == "AUTHORED" and tgt == CROSS_PERIOD_VOR_ID}
    assert {"A5000000028", "A5000000029"} <= authors
    assert multigroup.removed.publications == 1


def test_the_graph_pass_folds_a_repository_renamed_between_periods(multigroup):
    nodes, _labels, edges = multigroup.after_dedup
    assert OLD_GAMMA_REPO_ID not in nodes["Repository"]
    survivor = nodes["Repository"][GAMMA_REPO_ID]
    assert survivor["merged_ids"] == [OLD_GAMMA_REPO_ID]
    assert OLD_GAMMA_URL in survivor["cited_urls"]
    # The 2025 publication's link followed the node, not the URL it was
    # matched by.
    mentions = {(src, tgt) for (_src_label, rel, tgt_label, src, tgt) in edges
                if rel == "MENTIONS_LINK" and tgt_label == "Repository"}
    assert ("W80000002004", GAMMA_REPO_ID) in mentions
    assert multigroup.removed.repositories == 1


def test_the_graph_pass_holds_back_what_the_group_stage_held_back(multigroup):
    held = [row for row in multigroup.report if row["status"] == "held"]
    groups = [row["persons"] for row in held if "persons" in row]
    assert ["A5000000062", "A5000000063", "A5000000064"] in groups
    pairs = {frozenset((row["person_a"], row["person_b"])) for row in held if "person_a" in row}
    assert frozenset(("A5000000065", "A5000000066")) in pairs
    assert frozenset(("A5000000058", "A5000000059")) not in pairs  # explicit ORCIDs differ


def test_the_graph_pass_merges_nothing_else_and_says_why(multigroup):
    # Everything else was already deduplicated per group; a second, wider
    # pass must not start merging distinct entities, and every fold it does
    # apply has to be justified in the journal.
    merged = {(row.get("entity", "person"), row.get("person_a") or row.get("record_a")):
              row["rules"] for row in multigroup.report if row["status"] == "merged"}
    assert merged == {
        ("person", NIKITIN_2025_ID): ["orcid"],
        ("publication", CROSS_PERIOD_PREPRINT_ID): ["title"],
        ("repository", OLD_GAMMA_REPO_ID): ["github_id"],
    }


# --- republishing an old group ---------------------------------------------------

def test_republishing_a_folded_group_does_not_resurrect_its_duplicates(multigroup):
    # The 2025 group's JSONL still describes the person, the publication and
    # the repository the graph pass folded: the loader has to fold them
    # again, using the merged_ids the canonical nodes carry.
    assert multigroup.after_republish_2025 == multigroup.after_dedup


def test_republishing_every_group_keeps_the_folded_state(multigroup):
    nodes, labels, edges = multigroup.after_republish_both
    folded_nodes, folded_labels, folded_edges = multigroup.after_dedup
    assert NIKITIN_2025_ID not in nodes["Person"]
    assert CROSS_PERIOD_PREPRINT_ID not in nodes["Publication"]
    assert OLD_GAMMA_REPO_ID not in nodes["Repository"]
    assert {label: set(items) for label, items in nodes.items()} == \
        {label: set(items) for label, items in folded_nodes.items()}
    assert labels == folded_labels
    assert edges == folded_edges


def test_a_group_publish_overwrites_lists_the_graph_pass_folded_in(multigroup):
    # Known limitation, stated here so it is a decision and not a surprise:
    # node properties are replaced by whatever the group being published
    # knows, so list values a graph-wide merge folded in — the duplicate's
    # name variants, the URLs it was cited by — fall back to the owning
    # group's own view. The merge itself holds: merged_ids (see the test
    # above), the labels and every relationship survive.
    folded_nodes, _labels, _edges = multigroup.after_dedup
    nodes, _labels2, _edges2 = multigroup.after_republish_both
    assert folded_nodes["Person"][NIKITIN_2026_ID]["name_variants"] == \
        [NIKITIN_2025_NAME, "Nikolay Nikitin"]
    assert nodes["Person"][NIKITIN_2026_ID]["name_variants"] == [NIKITIN_2025_NAME]
    assert OLD_GAMMA_URL in folded_nodes["Repository"][GAMMA_REPO_ID]["cited_urls"]
    assert OLD_GAMMA_URL not in nodes["Repository"][GAMMA_REPO_ID]["cited_urls"]


def test_republishing_the_canonical_group_keeps_its_merge_record(multigroup):
    # The fold happened graph-wide, so the group that owns the surviving row
    # knows nothing about it: its JSONL carries an empty merged_ids. Writing
    # that back would erase the only record of the merge — and the next
    # publish of the other group would resurrect the duplicate for good.
    nodes, _labels, _edges = multigroup.after_republish_both
    assert nodes["Person"][NIKITIN_2026_ID]["merged_ids"] == [NIKITIN_2025_ID]
    assert nodes["Publication"][CROSS_PERIOD_VOR_ID]["merged_ids"] == [CROSS_PERIOD_PREPRINT_ID]
    assert nodes["Repository"][GAMMA_REPO_ID]["merged_ids"] == [OLD_GAMMA_REPO_ID]


def test_no_relationship_is_left_dangling(multigroup):
    assert multigroup.graph.unresolved == []


def test_merged_publications_of_the_group_stage_stay_merged(multigroup):
    nodes, _labels, _edges = multigroup.after_republish_both
    for merged_id in PUBLICATION_MERGES:
        assert merged_id not in nodes["Publication"]
