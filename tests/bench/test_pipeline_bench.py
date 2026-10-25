"""End-to-end pipeline run over the synthetic universe (no network, no DB).

The full conveyor - collect -> normalize -> enrich (all stages) -> graph
load - runs once per test module against mocked external services and an
in-memory Neo4j double; the tests below assert the invariants each tricky
case from tests/bench/universe.py is supposed to produce.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from unittest import mock

import mongomock
import pytest

from pauk.graph.jsonl_loader import load_prepared_rows, normalize_repo_url
from pauk.graph.load import ENTITY_FILES
from pauk.models import GitHubProfile, Person, Publication, RepoLink, Repository
from pauk.models.processing import ProcessingState, ProcessingStatus
from pauk.pipeline.collect import Collector
from pauk.pipeline.enrich import Enricher
from pauk.pipeline.normalize import OpenAlexNormalizer
from pauk.pipeline.selectors import PeriodSelector
from pauk.settings import Settings
from pauk.storage import PreparedStore, RawStore
from tests.bench.mocks import (
    MockCrossrefClient,
    MockGitHubClient,
    MockLinkRelevanceClient,
    MockOpenAlexClient,
    MockOpenRouterClient,
    MockOrcidClient,
    MockPdfHttpClient,
    RecordingNeo4jClient,
)
from tests.bench.universe import (
    ARCHIVE_AUTHOR_AFFILIATION,
    ARCHIVE_AUTHOR_ID,
    ARCHIVE_REPO_ID,
    ARCHIVE_REPO_URL,
    ARCHIVE_WORK,
    AUTHOR_IDS,
    CONSORTIUM_FILLERS,
    CONSORTIUM_ITMO_INDEX,
    CONSORTIUM_ORG_NAME,
    CONSORTIUM_WORK,
    DEDUP_MERGES,
    MARKUP_TITLE_CLEAN,
    MARKUP_WORK,
    ORG_CHILD_UID,
    ORG_NAME,
    ORG_UID,
    PHANTOM_URLS,
    REPO_OWNERS,
    RUSSIAN_NAMES_CATALOG,
    STALE_REPO_CANONICAL_ID,
    STALE_REPO_ID,
    STALE_REPO_PUBLICATION,
    STALE_REPO_URL,
    UNIDENTIFIED_BY_NAME,
    UNIDENTIFIED_ORCID,
    UNIDENTIFIED_WORK,
    UNTITLED_WORK_IDS,
    build_universe,
    repo_github_id,
)

GROUP = "bench"


def dept_id(name_en: str) -> str:
    # Department node id is the human-readable slug uid derived from name_en.
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", name_en.lower()).strip("-"))


def identifier_map(rows, scheme: str) -> dict[str, str]:
    """Map one provider's stable ids to opaque PAUK ids."""
    return {
        identifier.value: row.id
        for row in rows
        for identifier in row.identifiers
        if identifier.scheme == scheme
    }


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

    # This benchmark covers the legacy deterministic fixture. The enabled
    # resolver and its review routing have dedicated unit tests with model
    # verdicts, so the offline benchmark must not depend on an LLM API key.
    config = Settings(data_dir=data_dir, person_resolution_enabled=False)
    db = mongomock.MongoClient()["pauk_test"]
    raw = RawStore(db, GROUP)
    prepared = PreparedStore(db, GROUP)

    openalex = MockOpenAlexClient(universe)
    selector = PeriodSelector("2026-01-01", "2026-12-31")
    collected_first = Collector(openalex, raw).collect(selector)
    collected_again = Collector(openalex, raw).collect(selector)
    normalize_result = OpenAlexNormalizer(raw, prepared).run()
    normalized_publications = prepared.read_models("publications", Publication)
    normalized_publication_ids = identifier_map(normalized_publications, "openalex")

    github = MockGitHubClient(universe)
    patches = (
        mock.patch("pauk.pipeline.stages.repositories.GitHubClient", lambda *a, **k: github),
        # repo_people imports GitHubClient for itself; unpatched, the bench
        # spends the whole fixture on live calls to api.github.com.
        mock.patch("pauk.pipeline.stages.repo_people.GitHubClient", lambda *a, **k: github),
        mock.patch("pauk.pipeline.stages.persons.OpenAlexClient", lambda *a, **k: MockOpenAlexClient(universe)),
        mock.patch("pauk.pipeline.stages.persons.CrossrefClient", lambda *a, **k: MockCrossrefClient(universe)),
        mock.patch("pauk.pipeline.stages.persons.OrcidClient", lambda *a, **k: MockOrcidClient(universe)),
        mock.patch("pauk.pipeline.stages.code_links.HttpClient", lambda *a, **k: MockPdfHttpClient()),
        mock.patch("pauk.pipeline.stages.author_names.OpenRouterClient",
                   lambda *a, **k: MockOpenRouterClient(RUSSIAN_NAMES_CATALOG)),
        mock.patch("pauk.pipeline.stages.link_relevance.OpenRouterClient",
                   lambda *a, **k: MockLinkRelevanceClient()),
    )
    for p in patches:
        p.start()
    try:
        Enricher(prepared, raw, config).run()
        repos_after_first = {r.id: r for r in prepared.read_models("repositories", Repository)}
        # A repository renamed on GitHub after an earlier run: that run's row
        # kept the old id and URL, so only the numeric id can tie it to the
        # row the new name produced. Seeded here, folded by the re-run below.
        prepared.write_models("repositories", [*repos_after_first.values(), Repository(
            id=STALE_REPO_ID, name="legacy-name", url=STALE_REPO_URL,
            github_id=repo_github_id("BenchOrg7", "AlphaTool"),
            cited_urls=[STALE_REPO_URL], owner_login="BenchOrg7",
            publication_ids=[normalized_publication_ids[STALE_REPO_PUBLICATION]],
            _processing={"repositories": ProcessingState(status=ProcessingStatus.COMPLETED, attempts=1)},
        )])
        Enricher(prepared, raw, config).run()  # re-run: completed rows must not change
    finally:
        for p in patches:
            p.stop()

    def rows_by_file():
        return {filename: list(prepared.read_rows(entity)) for entity, filename in ENTITY_FILES.items()}

    client = RecordingNeo4jClient()
    load_prepared_rows(client, rows_by_file())
    snapshot_first = client.snapshot()
    load_prepared_rows(client, rows_by_file())
    snapshot_second = client.snapshot()

    publications = {p.id: p for p in prepared.read_models("publications", Publication)}
    persons = {p.id: p for p in prepared.read_models("persons", Person)}
    repo_links = {r.publication_id: r for r in prepared.read_models("repo_links", RepoLink)}
    publication_ids = identifier_map(publications.values(), "openalex")
    person_ids = identifier_map(persons.values(), "openalex")
    person_orcid_ids = identifier_map(persons.values(), "orcid")

    return SimpleNamespace(
        config=config,
        collected_first=collected_first,
        collected_again=collected_again,
        normalize_result=normalize_result,
        publications=publications,
        publication_ids=publication_ids,
        publication=lambda source_id: publications[publication_ids[source_id]],
        publication_id=lambda source_id: publication_ids[source_id],
        persons=persons,
        person_ids=person_ids,
        person=lambda source_id: persons[person_ids[source_id]],
        person_id=lambda source_id: person_ids[source_id],
        person_by_orcid=lambda orcid: persons[person_orcid_ids[orcid]],
        repositories={r.id: r for r in prepared.read_models("repositories", Repository)},
        repos_after_first=repos_after_first,
        profiles={p.id: p for p in prepared.read_models("github_profiles", GitHubProfile)},
        repo_links=repo_links,
        repo_link=lambda source_id: repo_links[publication_ids[source_id]],
        publication_review_pairs={
            frozenset(row["members"])
            for row in db.review_pairs.find({"kind": "publication_pair"}, {"members": 1})
        },
        graph=client,
        snapshot_first=snapshot_first,
        snapshot_second=snapshot_second,
    )




def test_collect_is_idempotent(bench):
    assert bench.collected_first == 124
    assert bench.collected_again == 0


def test_normalize_counts(bench):
    # Exact DOI identity is resolved during normalization. The other 123
    # source ids still resolve even though W111/W112 share one PAUK row.
    assert bench.normalize_result == {"publications": 123, "persons": 64}
    assert len(bench.publication_ids) == 124
    assert bench.publication_id("W70000000111") == bench.publication_id("W70000000112")

    # bench.persons is read after enrichment, i.e. after exact/person-resolution
    # merges. Every OpenAlex id remains routable to its canonical PAUK person.
    assert set(AUTHOR_IDS) <= set(bench.person_ids)
    assert set(CONSORTIUM_FILLERS) <= set(bench.person_ids)
    assert len(bench.persons) == 60


def test_flaky_affiliation_does_not_split_identity(bench):
    for i in range(1, 6):
        person = bench.person(f"A50000000{i:02d}")
        assert person.is_itmo, f"A{i:02d} must be ITMO despite missing affiliations"
    assert sum(p.is_itmo for p in bench.persons.values()) == 36


def test_duplicate_author_entry_kept_per_position(bench):
    a12 = bench.person("A5000000012")
    authored = [
        a for a in a12.authored
        if a.publication_id == bench.publication_id("W7000000018")
    ]
    # Neo4j has one AUTHORED edge per person/publication. The lossless source
    # ledger retains both occurrences and their original positions.
    assert len(authored) == 1
    source_authors = [
        author
        for record in bench.publication("W7000000018").source_records
        for author in record.authors
        if author.person_id == a12.id
    ]
    assert {author.position for author in source_authors} == {1, 3}


def test_untitled_and_dateless_work(bench):
    w13 = bench.publication("W7000000013")
    assert w13.title == "Untitled"
    assert w13.year is None and w13.publication_date is None




def test_url_junk_is_stripped(bench):
    assert json.loads(bench.publication("W7000000001").code_url) == [
        "https://github.com/BenchOrg1/AlphaTool"
    ]
    assert json.loads(bench.publication("W7000000002").code_url) == [
        "https://github.com/BenchOrg1/beta-kit"
    ]
    assert json.loads(bench.publication("W7000000003").code_url) == [
        "https://github.com/BenchOrg1/GammaLib"
    ]


def test_non_github_urls_are_ignored(bench):
    w11 = bench.publication("W7000000011")
    assert w11.has_code is False and w11.code_url is None
    assert bench.repo_link("W7000000011").links == []


def test_duplicate_url_in_one_abstract_deduplicated(bench):
    urls = [link.url for link in bench.repo_link("W7000000010").links]
    assert urls == ["https://github.com/BenchOrg1/delta.util", "https://github.com/BenchOrg1/EpsilonNet"]


def test_work_without_abstract_is_completed_empty(bench):
    w12 = bench.publication("W7000000012")
    assert w12.processing["code_links"].status == "completed_empty"
    assert w12.has_code is False




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
    assert {
        bench.publication_id("W7000000006"),
        bench.publication_id("W7000000007"),
    } <= set(repo.publication_ids)


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




def test_crossref_orcid_matching(bench):
    assert bench.person("A5000000014").orcid == "0000-0002-0000-0014"
    assert bench.person("A5000000014").email == "a14@example.org"
    assert bench.person("A5000000008").orcid == "0000-0005-0000-0008"  # hyphenated surname
    assert bench.person("A5000000006").orcid is None  # ambiguous family name
    assert bench.person("A5000000007").orcid is None
    assert bench.person("A5000000009").orcid is None  # multi-word surname: known limitation


def test_openalex_author_payload_enriches(bench):
    assert bench.person("A5000000010").name_raw == "Recovered Name10"
    assert bench.person("A5000000013").orcid == "0000-0001-0000-0013"
    variants = bench.person("A5000000011").name_variants
    assert "Хосе Альварес-Мюллер" in variants and "A. Surname11" in variants


def test_failing_author_endpoint_marks_failed(bench):
    state = bench.person("A5000000016").processing["openalex_author"]
    assert state.status == "failed" and "404" in state.error


def test_crossref_states(bench):
    assert bench.publication("W7000000014").processing["crossref"].status == "not_applicable"
    assert bench.publication("W7000000015").processing["crossref"].status == "failed"




def test_orcid_split_author_is_merged(bench):
    assert bench.person_id("A5000000051") == bench.person_id("A5000000052")
    canonical = bench.person("A5000000051")
    assert {i.value for i in canonical.identifiers if i.scheme == "openalex"} >= {
        "A5000000051", "A5000000052",
    }
    assert canonical.is_itmo, "ITMO flag must survive merging in the external half"
    assert {a.publication_id for a in canonical.authored} == {
        bench.publication_id("W70000000101"),
        bench.publication_id("W70000000102"),
    }
    assert "D. A. Kovalev" in canonical.name_variants


def test_variant_split_merges_transitively_including_cyrillic(bench):
    assert bench.person_id("A5000000053") == bench.person_id("A5000000054")
    assert bench.person_id("A5000000053") == bench.person_id("A5000000055")
    canonical = bench.person("A5000000053")
    assert {a.publication_id for a in canonical.authored} == {
        bench.publication_id("W70000000103"),
        bench.publication_id("W70000000104"),
        bench.publication_id("W70000000105"),
        bench.publication_id("W70000000110"),
    }
    assert {"E. Smirnova", "Екатерина Смирнова"} <= set(canonical.name_variants)


def test_same_name_without_distinguishing_marks_merges_by_default(bench):
    assert bench.person_id("A5000000056") == bench.person_id("A5000000057")
    canonical = bench.person("A5000000056")
    assert {a.publication_id for a in canonical.authored} == {
        bench.publication_id("W70000000106"),
        bench.publication_id("W70000000107"),
    }


def test_conflicting_orcids_stay_separate_and_unreported(bench):
    assert bench.person_id("A5000000058") != bench.person_id("A5000000059")
    candidates_path = bench.config.audit_dir / GROUP / "dedup_candidates.jsonl"
    rows = [json.loads(line) for line in candidates_path.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    pairs = {frozenset((row["person_a"], row["person_b"])) for row in rows}
    assert frozenset((
        bench.person_id("A5000000058"),
        bench.person_id("A5000000059"),
    )) not in pairs




def test_one_doi_re_indexed_twice_is_one_publication(bench):
    assert bench.publication_id("W70000000111") == bench.publication_id("W70000000112")
    survivor = bench.publication("W70000000111")
    assert {record.source_id for record in survivor.source_records} == {
        "W70000000111", "W70000000112",
    }
    assert {v.openalex_id for v in survivor.versions} == {"W70000000111", "W70000000112"}


def test_preprint_and_version_with_different_dois_stay_separate(bench):
    assert bench.publication_id("W70000000113") != bench.publication_id("W70000000114")
    preprint = bench.publication("W70000000113")
    article = bench.publication("W70000000114")
    assert (preprint.type, preprint.journal) == ("preprint", "Synthetic Preprint Server")
    assert (article.type, article.journal) == ("article", "Synthetic Journal")


def test_repeated_deposits_with_distinct_dois_stay_separate(bench):
    deposits = [bench.publication(f"W70000000{i}") for i in (115, 116, 117)]
    assert len({publication.id for publication in deposits}) == 3
    assert {publication.doi for publication in deposits} == {
        "https://doi.org/10.7777/deposit.115",
        "https://doi.org/10.7777/deposit.116",
        "https://doi.org/10.7777/deposit.117",
    }


def test_untitled_works_are_never_merged_with_each_other(bench):
    for work_id in UNTITLED_WORK_IDS:
        assert bench.publication(work_id).title == "Untitled"


def test_title_case_and_spacing_variants_with_distinct_dois_stay_separate(bench):
    first = bench.publication_id("W70000000119")
    second = bench.publication_id("W70000000120")
    assert first != second
    # Both records have distinct DOI values, so neither automatic identity
    # nor the title-without-DOI review rule may join them.
    assert frozenset((first, second)) not in bench.publication_review_pairs


def test_authorships_follow_both_distinct_publications(bench):
    for author_id in ("A5000000024", "A5000000025"):
        works = {
            a.publication_id
            for a in bench.person(author_id).authored
            if a.publication_id in {
                bench.publication_id("W70000000113"),
                bench.publication_id("W70000000114"),
            }
        }
        assert works == {
            bench.publication_id("W70000000113"),
            bench.publication_id("W70000000114"),
        }


def test_doi_aliases_address_one_graph_node(bench):
    canonical = bench.publication_id("W70000000111")
    assert canonical == bench.publication_id("W70000000112")
    assert canonical in bench.graph.nodes["Publication"]


def test_versions_are_json_text(bench):
    props = bench.graph.nodes["Publication"][bench.publication_id("W70000000114")]
    assert isinstance(props["versions"], str)
    assert isinstance(props["source_records"], str)
    versions = {entry["openalex_id"]: entry for entry in json.loads(props["versions"])}
    assert set(versions) == {"W70000000114"}
    expected_authors = {
        bench.person_id("A5000000024"),
        bench.person_id("A5000000025"),
    }
    source_records = json.loads(props["source_records"])
    assert {a["person_id"] for a in source_records[0]["authors"]} == expected_authors
    assert {a["person_id"] for a in versions["W70000000114"]["authors"]} == expected_authors




def test_authors_without_an_openalex_id_still_reach_the_graph(bench):
    work_id = bench.publication_id(UNIDENTIFIED_WORK)
    authors = [
        p for p in bench.persons.values()
        if any(a.publication_id == work_id for a in p.authored)
    ]
    # Three authorships, but the one carrying neither an id nor a name
    # cannot be keyed to anything.
    assert len(authors) == 2
    by_orcid = bench.person_by_orcid(UNIDENTIFIED_ORCID)
    assert by_orcid.orcid == UNIDENTIFIED_ORCID
    assert by_orcid.openalex_id is None and by_orcid.is_itmo
    by_name = next(p for p in authors if p.name_raw == UNIDENTIFIED_BY_NAME)
    assert by_name.id.startswith("per_") and by_name.openalex_id is None
    assert {(p.id, work_id) for p in authors} <= bench.graph.edge_pairs("AUTHORED")


def test_a_person_without_an_openalex_record_is_still_enriched(bench):
    # Having no author endpoint to call is not a failure, and the ORCID they
    # were keyed by is still worth following.
    person = bench.person_by_orcid(UNIDENTIFIED_ORCID)
    assert person.processing["openalex_author"].status == "not_applicable"
    orcid_state = person.processing["orcid"]
    assert orcid_state.status != "failed", orcid_state.error
    assert person.email == "no.id@example.org"


def test_publisher_markup_is_stripped_from_the_title(bench):
    assert bench.publication(MARKUP_WORK).title == MARKUP_TITLE_CLEAN


def test_consortium_paper_finds_the_itmo_participant_beyond_the_cut(bench):
    # The list endpoint truncated the author list; only the single-work
    # record names the ITMO participant.
    itmo_id = f"A50000000{CONSORTIUM_ITMO_INDEX}"
    publication_id = bench.publication_id(CONSORTIUM_WORK)
    assert (bench.person_id(itmo_id), publication_id) in bench.graph.edge_pairs("AUTHORED")
    for filler in CONSORTIUM_FILLERS:
        assert (bench.person_id(filler), publication_id) in bench.graph.edge_pairs("AUTHORED")


def test_an_organization_in_an_author_slot_never_becomes_a_person(bench):
    assert all(p.name_raw != CONSORTIUM_ORG_NAME for p in bench.persons.values())
    assert "A5900000999" not in bench.person_ids


def test_a_release_archive_points_at_the_repository_it_archives(bench):
    # The deposit cites no URL: the repository is named in its title.
    deposit = bench.publication(ARCHIVE_WORK)
    assert json.loads(deposit.code_url) == [ARCHIVE_REPO_URL]
    (link,) = bench.repo_link(ARCHIVE_WORK).links
    assert link.llm_reason == "repository_archived_by_this_deposit"
    repository = bench.repositories[ARCHIVE_REPO_ID]
    publication_id = bench.publication_id(ARCHIVE_WORK)
    assert publication_id in repository.publication_ids
    assert (ARCHIVE_REPO_ID, publication_id) in bench.graph.edge_pairs("IMPLEMENTS")


def test_an_affiliation_the_deposit_omits_comes_from_the_author_record(bench):
    (authorship,) = [
        a for a in bench.person(ARCHIVE_AUTHOR_ID).authored
        if a.publication_id == bench.publication_id(ARCHIVE_WORK)
    ]
    assert authorship.affiliation == ARCHIVE_AUTHOR_AFFILIATION
    assert authorship.affiliation_source == "openalex"
    # Re-running the pipeline must not undo the fill.
    assert bench.snapshot_first == bench.snapshot_second




def test_row_written_before_a_rename_folds_into_the_canonical_repository(bench):
    assert STALE_REPO_ID not in bench.repositories
    survivor = bench.repositories[STALE_REPO_CANONICAL_ID]
    assert survivor.merged_ids == [STALE_REPO_ID]
    assert survivor.url == "https://github.com/BenchOrg7/AlphaTool"
    # Both names stay citable, and the publication of the old row survives.
    assert STALE_REPO_URL in survivor.cited_urls
    assert bench.publication_id(STALE_REPO_PUBLICATION) in survivor.publication_ids



def test_russian_name_from_staff_catalog(bench):
    oleg = bench.person("A5000000006")
    assert oleg.name_ru == "Иванов Олег Петрович"
    assert (oleg.first_name_ru, oleg.second_name_ru, oleg.surname_ru) == ("Олег", "Петрович", "Иванов")
    assert oleg.degree == "к.т.н."


def test_russian_name_transliteration_fallback(bench):
    pavel = bench.person("A5000000007")  # "Pavel Ivanov" is not in the catalog
    # No directory match, so this is the LLM's own transcription rather than
    # a copied official record - still filled per rule 4 ("must ALWAYS be
    # filled in Cyrillic, for every person"), unlike the second_name
    # (patronymic) fields, which the model is never allowed to invent.
    assert (pavel.first_name_ru, pavel.surname_ru) == ("Павел", "Иванов")
    # name_ru is always composed surname-first (see RussianNamesStage.run()),
    # regardless of the word order the source name happened to use.
    assert pavel.name_ru == "Иванов Павел"
    assert pavel.second_name_ru is None




def test_department_matching_including_aliases(bench):
    assert bench.person("A5000000017").department_ids == [dept_id("Institute of Applied Computer Science")]
    assert bench.person("A5000000018").department_ids == [dept_id("Institute of Applied Computer Science")]
    assert bench.person("A5000000023").department_ids == [dept_id("Biotech Research Center")]
    assert bench.person("A5000000001").department_ids == []
    assert bench.person("A5000000031").department_ids == []




def test_graph_node_counts(bench):
    graph = bench.graph
    # Only exact DOI identity is automatic. Similar titles and different
    # deposits remain distinct until a human review says otherwise.
    assert len(graph.nodes["Publication"]) == 123
    # 55 OpenAlex authors, the two of W121 keyed by ORCID and by name,
    # and the consortium fillers.
    assert len(graph.nodes["Person"]) == 60
    assert len(graph.nodes["Repository"]) == 80
    assert len(graph.nodes["GitHubProfile"]) == 16
    assert len(graph.nodes["LinkCandidate"]) == 3
    itmo_count = sum(1 for props in graph.nodes["Person"].values() if props.get("is_itmo"))
    assert itmo_count == 36 and len(graph.nodes["Person"]) - itmo_count == 24
    for merged_id, canonical_id in DEDUP_MERGES.items():
        assert bench.person_id(merged_id) == bench.person_id(canonical_id)
        assert bench.person_id(canonical_id) in graph.nodes["Person"]


def test_link_candidates_are_exactly_the_phantoms(bench):
    assert set(bench.graph.nodes["LinkCandidate"]) == set(PHANTOM_URLS)


def test_graph_edge_counts(bench):
    graph = bench.graph
    expected_authored = {(person.id, a.publication_id) for person in bench.persons.values() for a in person.authored}
    assert graph.edge_pairs("AUTHORED") == expected_authored
    # Source-neutral publication identity retains all distinct DOI/deposit
    # records, adding eight authorships that title-based auto-merge removed.
    assert len(expected_authored) == 254

    assert len(graph.edge_pairs("MENTIONS_LINK")) == 83 + 3  # repos + candidates
    # 82 plus the publication inherited from the pre-rename repository row
    # and the release archive of W123.
    assert len(graph.edge_pairs("IMPLEMENTS")) == 84
    assert len(graph.edge_pairs("OWNED_BY")) == 80
    assert len(graph.edge_pairs("BELONGS_TO")) == 14


def test_organization_reaches_the_graph(bench):
    # Guards the ENTITY_FILES wiring in graph/load.py: the departments stage writes
    # an Organization row that must be loaded as a node, otherwise the
    # Department-[:PART_OF]->Organization edge silently never resolves.
    org = bench.graph.nodes["Organization"].get(ORG_UID)
    assert org is not None, "organization row never reached the graph"
    assert org["name_en"] == ORG_NAME
    assert (ORG_CHILD_UID, ORG_UID) in bench.graph.edge_pairs("PART_OF")


def test_every_relationship_resolved(bench):
    assert bench.graph.unresolved == []


def test_funding_is_json_text(bench):
    props = bench.graph.nodes["Publication"][bench.publication_id("W7000000019")]
    assert isinstance(props["funding"], str) and "Synthetic Science Fund" in props["funding"]


def test_processing_never_leaks_into_graph(bench):
    for label, nodes in bench.graph.nodes.items():
        for node_id, props in nodes.items():
            assert "_processing" not in props, f"{label}/{node_id}"


def test_graph_load_is_idempotent(bench):
    assert bench.snapshot_first == bench.snapshot_second
