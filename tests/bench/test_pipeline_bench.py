"""End-to-end pipeline run over the synthetic universe (no network, no DB).

The full conveyor — collect -> normalize -> enrich (all stages) -> graph
load — runs once per test module against mocked external services and an
in-memory Neo4j double; the tests below assert the invariants each tricky
case from tests/bench/universe.py is supposed to produce.
"""

from __future__ import annotations

import json
import re
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
    ARCHIVE_AUTHOR_AFFILIATION,
    CONSORTIUM_FILLERS,
    CONSORTIUM_ITMO_INDEX,
    CONSORTIUM_ORG_NAME,
    CONSORTIUM_WORK,
    ARCHIVE_AUTHOR_ID,
    ARCHIVE_REPO_ID,
    ARCHIVE_REPO_URL,
    ARCHIVE_WORK,
    AUTHOR_IDS,
    DEDUP_MERGES,
    MARKUP_TITLE_CLEAN,
    MARKUP_WORK,
    PHANTOM_URLS,
    PUBLICATION_MERGES,
    REPO_OWNERS,
    STALE_REPO_CANONICAL_ID,
    STALE_REPO_ID,
    STALE_REPO_PUBLICATION,
    STALE_REPO_URL,
    UNIDENTIFIED_BY_NAME,
    UNIDENTIFIED_BY_ORCID,
    UNIDENTIFIED_ORCID,
    UNIDENTIFIED_WORK,
    UNTITLED_WORK_IDS,
    RUSSIAN_NAMES_CATALOG,
    build_universe,
    repo_github_id,
)

GROUP = "bench"


def dept_id(name_en: str) -> str:
    # Department node id is the human-readable slug uid derived from name_en.
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", name_en.lower()).strip("-"))


@pytest.fixture(scope="module")
def bench(tmp_path_factory) -> SimpleNamespace:
    data_dir = tmp_path_factory.mktemp("bench-data")
    (data_dir / "static").mkdir()
    universe = build_universe()
    (data_dir / "static" / "departments_catalog.json").write_text(
        json.dumps(universe["departments_catalog"], ensure_ascii=False), encoding="utf-8")
    (data_dir / "static" / "russian_names.csv").write_text(
        "name_ru,surname,name,patronymic,degree\n"
        + "".join(f"{row}\n" for row in RUSSIAN_NAMES_CATALOG), encoding="utf-8")

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
        # A repository renamed on GitHub after an earlier run: that run's row
        # kept the old id and URL, so only the numeric id can tie it to the
        # row the new name produced. Seeded here, folded by the re-run below.
        prepared.write_models(
            "repositories",
            [
                *repos_after_first.values(),
                Repository(
                    id=STALE_REPO_ID,
                    name="legacy-name",
                    url=STALE_REPO_URL,
                    github_id=repo_github_id("BenchOrg7", "AlphaTool"),
                    cited_urls=[STALE_REPO_URL],
                    owner_login="BenchOrg7",
                    publication_ids=[STALE_REPO_PUBLICATION],
                    processing={"repositories": {"status": "completed", "attempts": 1}},
                ),
            ],
        )
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
    assert bench.collected_first == 124
    assert bench.collected_again == 0


def test_normalize_counts(bench):
    # Normalization keeps one row per OpenAlex work; duplicate records of one
    # publication are folded later, by the dedup stage.
    assert bench.normalize_result == {"publications": 124, "persons": 64}
    # bench.persons is read after enrichment, i.e. after the dedup stage
    # folded the split authors away.
    openalex_persons = {pid for pid in bench.persons if pid.startswith("A50")}
    assert openalex_persons == set(AUTHOR_IDS) - set(DEDUP_MERGES)
    assert set(CONSORTIUM_FILLERS) <= set(bench.persons)
    # Plus the consortium fillers and the two authors of W121, whom
    # OpenAlex has not disambiguated.
    assert len(bench.persons) == len(openalex_persons) + len(CONSORTIUM_FILLERS) + 2


def test_flaky_affiliation_does_not_split_identity(bench):
    for i in range(1, 6):
        person = bench.persons[f"A50000000{i:02d}"]
        assert person.is_itmo, f"A{i:02d} must be ITMO despite missing affiliations"
    assert sum(p.is_itmo for p in bench.persons.values()) == 36


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
    completed = [r for r in bench.repositories.values() if r.processing["repositories"].status == "completed"]
    failed = [r for r in bench.repositories.values() if r.processing["repositories"].status == "failed"]
    assert len(completed) == 80
    assert len(failed) == 3
    urls = [r.url for r in completed]
    assert len(urls) == len(set(urls)), "two repository rows share one URL"


def test_case_variants_resolve_to_one_repository(bench):
    matches = [
        r for r in bench.repositories.values() if normalize_repo_url(r.url) == "https://github.com/benchorg2/gammalib"
    ]
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


# --- dedup ------------------------------------------------------------------------------


def test_orcid_split_author_is_merged(bench):
    assert "A5000000052" not in bench.persons
    canonical = bench.persons["A5000000051"]
    assert canonical.merged_ids == ["A5000000052"]
    assert canonical.is_itmo, "ITMO flag must survive merging in the external half"
    assert {a.publication_id for a in canonical.authored} == {"W70000000101", "W70000000102"}
    assert "D. A. Kovalev" in canonical.name_variants


def test_variant_split_merges_transitively_including_cyrillic(bench):
    assert "A5000000054" not in bench.persons
    assert "A5000000055" not in bench.persons
    canonical = bench.persons["A5000000053"]
    assert canonical.merged_ids == ["A5000000054", "A5000000055"]
    assert {a.publication_id for a in canonical.authored} == {
        "W70000000103",
        "W70000000104",
        "W70000000105",
        "W70000000110",
    }
    assert {"E. Smirnova", "Екатерина Смирнова"} <= set(canonical.name_variants)


def test_same_name_without_distinguishing_marks_merges_by_default(bench):
    assert "A5000000057" not in bench.persons
    canonical = bench.persons["A5000000056"]
    assert canonical.merged_ids == ["A5000000057"]
    assert {a.publication_id for a in canonical.authored} == {"W70000000106", "W70000000107"}


def test_conflicting_orcids_stay_separate_and_unreported(bench):
    assert "A5000000058" in bench.persons and "A5000000059" in bench.persons
    candidates_path = bench.config.prepared_dir / GROUP / "dedup_candidates.jsonl"
    rows = [json.loads(line) for line in candidates_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    pairs = {frozenset((row["person_a"], row["person_b"])) for row in rows}
    assert frozenset(("A5000000058", "A5000000059")) not in pairs


# --- publication dedup -------------------------------------------------------------------


def test_one_doi_re_indexed_twice_is_one_publication(bench):
    assert "W70000000112" not in bench.publications
    survivor = bench.publications["W70000000111"]
    assert survivor.merged_ids == ["W70000000112"]
    assert {v.openalex_id for v in survivor.versions} == {"W70000000111", "W70000000112"}


def test_preprint_folds_into_version_of_record_keeping_both_venues(bench):
    assert "W70000000113" not in bench.publications
    survivor = bench.publications["W70000000114"]
    assert survivor.type == "article"
    assert (survivor.journal, survivor.doi) == ("Synthetic Journal", "https://doi.org/10.7777/vor.114")
    assert {(v.journal, v.doi) for v in survivor.versions} == {
        ("Synthetic Journal", "https://doi.org/10.7777/vor.114"),
        ("Synthetic Preprint Server", "https://doi.org/10.7777/preprint.113"),
    }


def test_repeated_deposits_collapse_into_the_newest_one(bench):
    survivor = bench.publications["W70000000117"]
    assert sorted(survivor.merged_ids) == ["W70000000115", "W70000000116"]
    assert {v.doi for v in survivor.versions} == {
        "https://doi.org/10.7777/deposit.115",
        "https://doi.org/10.7777/deposit.116",
        "https://doi.org/10.7777/deposit.117",
    }


def test_untitled_works_are_never_merged_with_each_other(bench):
    for work_id in UNTITLED_WORK_IDS:
        assert bench.publications[work_id].title == "Untitled"


def test_title_case_and_spacing_variants_are_one_publication(bench):
    assert "W70000000120" not in bench.publications
    assert bench.publications["W70000000119"].title == "Bench Case Variant Study"


def test_authorships_follow_the_surviving_publication(bench):
    for author_id in ("A5000000024", "A5000000025"):
        works = [
            a.publication_id
            for a in bench.persons[author_id].authored
            if a.publication_id in {"W70000000113", "W70000000114"}
        ]
        # One authorship, not one per merged record.
        assert works == ["W70000000114"]


def test_merged_publications_leave_no_graph_node(bench):
    for merged_id in PUBLICATION_MERGES:
        assert merged_id not in bench.graph.nodes["Publication"]


def test_versions_are_json_text(bench):
    props = bench.graph.nodes["Publication"]["W70000000114"]
    assert isinstance(props["versions"], str)
    assert "Synthetic Preprint Server" in props["versions"]
    # Each version entry records the author list that record itself carried.
    versions = {entry["openalex_id"]: entry for entry in json.loads(props["versions"])}
    assert {a["person_id"] for a in versions["W70000000113"]["authors"]} == {"A5000000024", "A5000000025"}


# --- records OpenAlex has not finished processing ------------------------------------------


def test_authors_without_an_openalex_id_still_reach_the_graph(bench):
    authors = [p for p in bench.persons.values() if any(a.publication_id == UNIDENTIFIED_WORK for a in p.authored)]
    # Three authorships, but the one carrying neither an id nor a name
    # cannot be keyed to anything.
    assert len(authors) == 2
    by_orcid = bench.persons[UNIDENTIFIED_BY_ORCID]
    assert by_orcid.orcid == UNIDENTIFIED_ORCID
    assert by_orcid.openalex_id is None and by_orcid.is_itmo
    by_name = next(p for p in authors if p.name_en == UNIDENTIFIED_BY_NAME)
    assert by_name.id.startswith("name_") and by_name.openalex_id is None
    assert {(p.id, UNIDENTIFIED_WORK) for p in authors} <= bench.graph.edge_pairs("AUTHORED")


def test_a_person_without_an_openalex_record_is_still_enriched(bench):
    # Having no author endpoint to call is not a failure, and the ORCID they
    # were keyed by is still worth following.
    person = bench.persons[UNIDENTIFIED_BY_ORCID]
    state = person.processing["persons"]
    assert state.status != "failed", state.error
    assert person.email == "no.id@example.org"


def test_publisher_markup_is_stripped_from_the_title(bench):
    assert bench.publications[MARKUP_WORK].title == MARKUP_TITLE_CLEAN


def test_consortium_paper_finds_the_itmo_participant_beyond_the_cut(bench):
    # The list endpoint truncated the author list; only the single-work
    # record names the ITMO participant.
    itmo_id = f"A50000000{CONSORTIUM_ITMO_INDEX}"
    assert (itmo_id, CONSORTIUM_WORK) in bench.graph.edge_pairs("AUTHORED")
    for filler in CONSORTIUM_FILLERS:
        assert (filler, CONSORTIUM_WORK) in bench.graph.edge_pairs("AUTHORED")


def test_an_organization_in_an_author_slot_never_becomes_a_person(bench):
    assert all(p.name_en != CONSORTIUM_ORG_NAME for p in bench.persons.values())
    assert "A5900000999" not in bench.persons


def test_a_release_archive_points_at_the_repository_it_archives(bench):
    # The deposit cites no URL: the repository is named in its title.
    deposit = bench.publications[ARCHIVE_WORK]
    assert deposit.code_url == ARCHIVE_REPO_URL
    (link,) = bench.repo_links[ARCHIVE_WORK].links
    assert link.llm_reason == "repository_archived_by_this_deposit"
    repository = bench.repositories[ARCHIVE_REPO_ID]
    assert ARCHIVE_WORK in repository.publication_ids
    assert (ARCHIVE_REPO_ID, ARCHIVE_WORK) in bench.graph.edge_pairs("IMPLEMENTS")


def test_an_affiliation_the_deposit_omits_comes_from_the_author_record(bench):
    (authorship,) = [a for a in bench.persons[ARCHIVE_AUTHOR_ID].authored if a.publication_id == ARCHIVE_WORK]
    assert authorship.affiliation == ARCHIVE_AUTHOR_AFFILIATION
    assert authorship.affiliation_source == "openalex"
    # Re-running the pipeline must not undo the fill.
    assert bench.snapshot_first == bench.snapshot_second


# --- repository dedup --------------------------------------------------------------------


def test_row_written_before_a_rename_folds_into_the_canonical_repository(bench):
    assert STALE_REPO_ID not in bench.repositories
    survivor = bench.repositories[STALE_REPO_CANONICAL_ID]
    assert survivor.merged_ids == [STALE_REPO_ID]
    assert survivor.url == "https://github.com/BenchOrg7/AlphaTool"
    # Both names stay citable, and the publication of the old row survives.
    assert STALE_REPO_URL in survivor.cited_urls
    assert STALE_REPO_PUBLICATION in survivor.publication_ids


# --- russian names -----------------------------------------------------------------------

def test_russian_name_from_staff_catalog(bench):
    oleg = bench.persons["A5000000006"]
    assert oleg.name_ru == "Иванов Олег Петрович"
    assert (oleg.first_name_ru, oleg.second_name_ru, oleg.surname_ru) == ("Олег", "Петрович", "Иванов")
    assert oleg.degree == "к.т.н."


def test_russian_name_transliteration_fallback(bench):
    pavel = bench.persons["A5000000007"]  # "Pavel Ivanov" is not in the catalog
    assert pavel.name_ru == "Павел Иванов"
    assert pavel.surname_ru is None  # parts are never guessed from word order


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
    assert len(graph.nodes["Publication"]) == 124 - len(PUBLICATION_MERGES)
    # 55 OpenAlex authors, the two of W121 keyed by ORCID and by name,
    # and the consortium fillers.
    assert len(graph.nodes["Person"]) == 60
    assert len(graph.nodes["Repository"]) == 80
    assert len(graph.nodes["GitHubProfile"]) == 16
    assert len(graph.nodes["LinkCandidate"]) == 3
    labels = list(graph.person_labels.values())
    assert labels.count("Itmo") == 36 and labels.count("External") == 24
    for merged_id in DEDUP_MERGES:
        assert merged_id not in graph.nodes["Person"]


def test_link_candidates_are_exactly_the_phantoms(bench):
    assert set(bench.graph.nodes["LinkCandidate"]) == set(PHANTOM_URLS)


def test_graph_edge_counts(bench):
    graph = bench.graph
    expected_authored = {(person.id, a.publication_id) for person in bench.persons.values() for a in person.authored}
    assert graph.edge_pairs("AUTHORED") == expected_authored
    # 228 from works W001..W110, 9 from the duplicate-record works (two
    # authors each on W111, W114, W117 and W119, one on W118), 2 keyable
    # authors on W121, 2 on W122, the lone depositor of W123, and the
    # consortium paper's 3 fillers + ITMO participant.
    assert len(expected_authored) == 246

    assert len(graph.edge_pairs("MENTIONS_LINK")) == 83 + 3  # repos + candidates
    # 82 plus the publication inherited from the pre-rename repository row
    # and the release archive of W123.
    assert len(graph.edge_pairs("IMPLEMENTS")) == 84
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
