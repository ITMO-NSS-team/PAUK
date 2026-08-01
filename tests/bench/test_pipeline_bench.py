"""End-to-end pipeline run over the synthetic universe (no network, no DB).

The full conveyor — collect -> normalize -> enrich (all stages) -> graph
load — runs once per test module against mocked external services and an
in-memory Neo4j double; the tests below assert the invariants each tricky
case from tests/bench/universe.py is supposed to produce.
"""

from __future__ import annotations

import json
from hashlib import sha256
from types import SimpleNamespace
from unittest import mock

import pytest

from pauk.graph.jsonl_loader import load_jsonl_dir, normalize_repo_url
from pauk.models import GitHubProfile, Person, Publication, RepoLink, Repository
from pauk.pipeline.collect import Collector
from pauk.pipeline.enrich import Enricher
from pauk.pipeline.normalize import OpenAlexNormalizer
from pauk.pipeline.selectors import PeriodSelector
from pauk.settings import Settings
from pauk.storage import PreparedStore, RawStore
from tests.bench.mocks import (
    MockCrossrefClient,
    MockGitHubClient,
    MockOpenAlexClient,
    MockOrcidClient,
    RecordingNeo4jClient,
    UnexpectedNetworkClient,
)
from tests.bench.universe import (
    AUTHOR_IDS,
    PHANTOM_URLS,
    REPO_OWNERS,
    build_universe,
)

GROUP = "bench"


def dept_id(name_en: str) -> str:
    return f"dept_{sha256(name_en.casefold().encode()).hexdigest()[:12]}"


@pytest.fixture(scope="module")
def bench(tmp_path_factory) -> SimpleNamespace:
    data_dir = tmp_path_factory.mktemp("bench-data")
    (data_dir / "static").mkdir()
    universe = build_universe()
    (data_dir / "static" / "departments_catalog.json").write_text(
        json.dumps(universe["departments_catalog"], ensure_ascii=False), encoding="utf-8")

    config = Settings(data_dir=data_dir)
    raw = RawStore(config.raw_dir, GROUP)
    prepared = PreparedStore(config.prepared_dir, GROUP)

    openalex = MockOpenAlexClient(universe)
    selector = PeriodSelector("2026-01-01", "2026-12-31")
    collected_first = Collector(openalex, raw).collect(selector)
    collected_again = Collector(openalex, raw).collect(selector)
    normalize_result = OpenAlexNormalizer(raw, prepared).run()

    github = MockGitHubClient(universe)
    patches = (
        mock.patch("pauk.pipeline.stages.repositories.GitHubClient", lambda *a, **k: github),
        mock.patch("pauk.pipeline.stages.persons.OpenAlexClient", lambda *a, **k: MockOpenAlexClient(universe)),
        mock.patch("pauk.pipeline.stages.persons.CrossrefClient", lambda *a, **k: MockCrossrefClient(universe)),
        mock.patch("pauk.pipeline.stages.persons.OrcidClient", lambda *a, **k: MockOrcidClient(universe)),
        mock.patch("pauk.pipeline.stages.persons.OpenReviewClient", lambda *a, **k: UnexpectedNetworkClient()),
    )
    for p in patches:
        p.start()
    try:
        Enricher(prepared, raw, config).run()
        repos_after_first = {r.id: r for r in prepared.read_models("repositories", Repository)}
        Enricher(prepared, raw, config).run()  # re-run: completed rows must not change
    finally:
        for p in patches:
            p.stop()

    client = RecordingNeo4jClient()
    load_jsonl_dir(client, config.prepared_dir / GROUP)
    snapshot_first = client.snapshot()
    load_jsonl_dir(client, config.prepared_dir / GROUP)
    snapshot_second = client.snapshot()

    return SimpleNamespace(
        config=config,
        collected_first=collected_first,
        collected_again=collected_again,
        normalize_result=normalize_result,
        publications={p.id: p for p in prepared.read_models("publications", Publication)},
        persons={p.id: p for p in prepared.read_models("persons", Person)},
        repositories={r.id: r for r in prepared.read_models("repositories", Repository)},
        repos_after_first=repos_after_first,
        profiles={p.id: p for p in prepared.read_models("github_profiles", GitHubProfile)},
        repo_links={r.publication_id: r for r in prepared.read_models("repo_links", RepoLink)},
        graph=client,
        snapshot_first=snapshot_first,
        snapshot_second=snapshot_second,
    )


# --- collect / normalize -------------------------------------------------------

def test_collect_is_idempotent(bench):
    assert bench.collected_first == 100
    assert bench.collected_again == 0


def test_normalize_counts(bench):
    assert bench.normalize_result == {"publications": 100, "persons": 50}
    assert set(bench.persons) == set(AUTHOR_IDS)


def test_flaky_affiliation_does_not_split_identity(bench):
    for i in range(1, 6):
        person = bench.persons[f"A50000000{i:02d}"]
        assert person.is_itmo, f"A{i:02d} must be ITMO despite missing affiliations"
    assert sum(p.is_itmo for p in bench.persons.values()) == 30


def test_duplicate_author_entry_kept_per_position(bench):
    a12 = bench.persons["A5000000012"]
    w18_records = [a for a in a12.authored if a.publication_id == "W7000000018"]
    assert len(w18_records) == 2
    assert {a.position for a in w18_records} == {1, 3}


def test_untitled_and_dateless_work(bench):
    w13 = bench.publications["W7000000013"]
    assert w13.title == "Untitled"
    assert w13.year is None and w13.publication_date is None


# --- code_links -----------------------------------------------------------------

def test_url_junk_is_stripped(bench):
    assert bench.publications["W7000000001"].code_url == "https://github.com/BenchOrg1/AlphaTool"
    assert bench.publications["W7000000002"].code_url == "https://github.com/BenchOrg1/beta-kit"
    assert bench.publications["W7000000003"].code_url == "https://github.com/BenchOrg1/GammaLib"


def test_non_github_urls_are_ignored(bench):
    w11 = bench.publications["W7000000011"]
    assert w11.has_code is False and w11.code_url is None
    assert bench.repo_links["W7000000011"].links == []


def test_duplicate_url_in_one_abstract_deduplicated(bench):
    urls = [link.url for link in bench.repo_links["W7000000010"].links]
    assert urls == ["https://github.com/BenchOrg1/delta.util", "https://github.com/BenchOrg1/EpsilonNet"]


def test_work_without_abstract_is_completed_empty(bench):
    w12 = bench.publications["W7000000012"]
    assert w12.processing["code_links"].status == "completed_empty"
    assert w12.has_code is False


# --- repositories -----------------------------------------------------------------

def test_exactly_80_repositories_enriched(bench):
    completed = [r for r in bench.repositories.values()
                 if r.processing["repositories"].status == "completed"]
    failed = [r for r in bench.repositories.values()
              if r.processing["repositories"].status == "failed"]
    assert len(completed) == 80
    assert len(failed) == 3
    urls = [r.url for r in completed]
    assert len(urls) == len(set(urls)), "two repository rows share one URL"


def test_case_variants_resolve_to_one_repository(bench):
    matches = [r for r in bench.repositories.values()
               if normalize_repo_url(r.url) == "https://github.com/benchorg2/gammalib"]
    assert len(matches) == 1
    assert matches[0].url == "https://github.com/BenchOrg2/GammaLib"


def test_renamed_repo_merges_into_canonical(bench):
    ids = [rid for rid in bench.repositories if "benchorg3" in rid and "alpha" in rid]
    assert ids == ["github_benchorg3_alphatool"], f"renamed repo split into {ids}"
    repo = bench.repositories["github_benchorg3_alphatool"]
    assert {"W7000000006", "W7000000007"} <= set(repo.publication_ids)


def test_www_citation_is_a_github_repo(bench):
    repo = bench.repositories.get("github_benchorg4_alphatool")
    assert repo is not None, "www.github.com citation was not recognised"
    assert repo.processing["repositories"].status == "completed"


def test_deleted_repos_fail_and_stay_failed(bench):
    for rid in ("github_goneorg_vanished-repo", "github_goneorg_never-was", "github_benchorg5_typo-name"):
        assert bench.repositories[rid].processing["repositories"].status == "failed"
        assert "404" in bench.repositories[rid].processing["repositories"].error


def test_second_enrich_run_leaves_completed_repos_alone(bench):
    for rid, repo in bench.repositories.items():
        state = repo.processing["repositories"]
        if state.status == "completed":
            first = bench.repos_after_first.get(rid)
            assert first is not None, f"{rid} appeared only on the second run"
            assert state.attempts == first.processing["repositories"].attempts


def test_github_profiles_one_per_owner(bench):
    assert len(bench.profiles) == len(REPO_OWNERS) == 16
    assert {p.login for p in bench.profiles.values()} == set(REPO_OWNERS)


# --- persons enrichment --------------------------------------------------------------

def test_crossref_orcid_matching(bench):
    assert bench.persons["A5000000014"].orcid == "0000-0002-0000-0014"
    assert bench.persons["A5000000014"].email == "a14@example.org"
    assert bench.persons["A5000000008"].orcid == "0000-0005-0000-0008"  # hyphenated surname
    assert bench.persons["A5000000006"].orcid is None  # ambiguous family name
    assert bench.persons["A5000000007"].orcid is None
    assert bench.persons["A5000000009"].orcid is None  # multi-word surname: known limitation


def test_openalex_author_payload_enriches(bench):
    assert bench.persons["A5000000010"].name_en == "Recovered Name10"
    assert bench.persons["A5000000013"].orcid == "0000-0001-0000-0013"
    variants = bench.persons["A5000000011"].name_variants
    assert "Хосе Альварес-Мюллер" in variants and "A. Surname11" in variants


def test_failing_author_endpoint_marks_failed(bench):
    state = bench.persons["A5000000016"].processing["persons"]
    assert state.status == "failed" and "404" in state.error


def test_crossref_states(bench):
    assert bench.publications["W7000000014"].processing["crossref"].status == "not_applicable"
    assert bench.publications["W7000000015"].processing["crossref"].status == "failed"


# --- departments -----------------------------------------------------------------------

def test_department_matching_including_aliases(bench):
    assert bench.persons["A5000000017"].department_ids == [dept_id("Institute of Applied Computer Science")]
    assert bench.persons["A5000000018"].department_ids == [dept_id("Institute of Applied Computer Science")]
    assert bench.persons["A5000000023"].department_ids == [dept_id("Biotech Research Center")]
    assert bench.persons["A5000000001"].department_ids == []
    assert bench.persons["A5000000031"].department_ids == []


# --- graph load --------------------------------------------------------------------------

def test_graph_node_counts(bench):
    graph = bench.graph
    assert len(graph.nodes["Publication"]) == 100
    assert len(graph.nodes["Person"]) == 50
    assert len(graph.nodes["Repository"]) == 80
    assert len(graph.nodes["GitHubProfile"]) == 16
    assert len(graph.nodes["LinkCandidate"]) == 3
    labels = list(graph.person_labels.values())
    assert labels.count("Itmo") == 30 and labels.count("External") == 20


def test_link_candidates_are_exactly_the_phantoms(bench):
    assert set(bench.graph.nodes["LinkCandidate"]) == set(PHANTOM_URLS)


def test_graph_edge_counts(bench):
    graph = bench.graph
    expected_authored = {(person.id, a.publication_id)
                         for person in bench.persons.values() for a in person.authored}
    assert graph.edge_pairs("AUTHORED") == expected_authored
    assert len(expected_authored) == 208

    assert len(graph.edge_pairs("MENTIONS_LINK")) == 82 + 3  # repos + candidates
    assert len(graph.edge_pairs("IMPLEMENTS")) == 82
    assert len(graph.edge_pairs("OWNED_BY")) == 80
    assert len(graph.edge_pairs("BELONGS_TO")) == 14


def test_every_relationship_resolved(bench):
    assert bench.graph.unresolved == []


def test_funding_is_json_text(bench):
    props = bench.graph.nodes["Publication"]["W7000000019"]
    assert isinstance(props["funding"], str) and "Synthetic Science Fund" in props["funding"]


def test_processing_never_leaks_into_graph(bench):
    for label, nodes in bench.graph.nodes.items():
        for node_id, props in nodes.items():
            assert "_processing" not in props, f"{label}/{node_id}"


def test_graph_load_is_idempotent(bench):
    assert bench.snapshot_first == bench.snapshot_second
