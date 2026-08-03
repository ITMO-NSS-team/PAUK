"""End-to-end pipeline run over the synthetic universe (no network, no DB).

The full conveyor runs once per test module against mocked external services
and an in-memory Neo4j double, in the order a real deployment would hit it:

    collect (period) -> normalize -> enrich -> publish
                     -> collect (a later works file) -> normalize
                     -> enrich again -> publish -> publish
                     -> enrich --force -> publish

Two external services fail on the first pass and answer on the second (see
RATE_LIMITED_ONCE and FLAKY_ORCIDS in tests/bench/universe.py), so the run
also covers what a resumed pipeline has to repair. The tests below assert
the invariants each tricky case from tests/bench/universe.py produces.
"""

from __future__ import annotations

import json
from collections import Counter
from hashlib import sha256
from types import SimpleNamespace

import pytest

from pauk.graph.jsonl_loader import load_jsonl_dir, normalize_repo_url
from pauk.models import GitHubProfile, Person, Publication, RepoLink, Repository
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
from tests.bench.mocks import MockGitHubClient, MockOpenAlexClient, MockOrcidClient, RecordingNeo4jClient
from tests.bench.universe import (
    AUTHOR_IDS,
    DEDUP_MERGES,
    FLAKY_ORCIDS,
    FLAKY_REPO_ID,
    FLAKY_REPO_URL,
    INCREMENTAL_WORK_IDS,
    NAMESAKE_ORCID,
    ORPHAN_REPO_ID,
    PHANTOM_2,
    PHANTOM_URLS,
    PUBLICATION_MERGES,
    RATE_LIMITED_ONCE,
    REPO_OWNERS,
    SOKOLOV_ORCID,
    STALE_REPO_CANONICAL_ID,
    STALE_REPO_ID,
    STALE_REPO_PUBLICATION,
    STALE_REPO_URL,
    UNTITLED_WORK_IDS,
    build_universe,
    expected_authorship_pairs,
    repo_github_id,
)

GROUP = "bench"

PERIOD_WORKS = 135          # the 2026 period; W134..W138 are dated 2027
TOTAL_WORKS = 140
TOTAL_AUTHORS = 71
# Duplicate records folded by the first enrichment run: W134/W135 are not
# collected yet, so their merge only happens on the second run.
FIRST_RUN_PUBLICATION_MERGES = 6


def dept_id(name_en: str) -> str:
    return f"dept_{sha256(name_en.casefold().encode()).hexdigest()[:12]}"


def read_journal(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def prepared_state(prepared: PreparedStore) -> SimpleNamespace:
    return SimpleNamespace(
        publications={p.id: p for p in prepared.read_models("publications", Publication)},
        persons={p.id: p for p in prepared.read_models("persons", Person)},
        repositories={r.id: r for r in prepared.read_models("repositories", Repository)},
    )


@pytest.fixture(scope="module")
def bench(tmp_path_factory) -> SimpleNamespace:
    data_dir = tmp_path_factory.mktemp("bench-data")
    universe = build_universe()
    write_static_catalog(data_dir, universe["departments_catalog"])

    config = bench_settings(data_dir)
    raw = RawStore(config.raw_dir, GROUP)
    prepared = PreparedStore(config.prepared_dir, GROUP)
    openalex = MockOpenAlexClient(universe)
    # One client per external service for the whole run: the transient
    # failures they are configured with must heal on the *second* call, not
    # on every fresh instance.
    github = MockGitHubClient(universe, rate_limited_once=RATE_LIMITED_ONCE)
    orcid = MockOrcidClient(universe, unavailable_once=FLAKY_ORCIDS)

    # --- first pass: one period, everything the API will serve for it ------
    period = PeriodSelector("2026-01-01", "2026-12-31")
    collected_first = Collector(openalex, raw).collect(period)
    collected_again = Collector(openalex, raw).collect(period)
    normalize_first = OpenAlexNormalizer(raw, prepared).run()

    with external_services(universe, github, orcid):
        Enricher(prepared, raw, config).run()
    after_first = prepared_state(prepared)
    candidates_first = read_journal(prepared.group_dir / "dedup_candidates.jsonl")

    client = RecordingNeo4jClient()
    load_jsonl_dir(client, config.prepared_dir / GROUP)
    after_first_publish = SimpleNamespace(
        repositories=set(client.nodes["Repository"]),
        candidates=set(client.nodes["LinkCandidate"]),
        mentions=client.edge_pairs("MENTIONS_LINK"),
    )

    # --- second pass: works of a later period arrive by file ---------------
    incremental = WorksFileSelector(works_file(data_dir, "works.txt", INCREMENTAL_WORK_IDS))
    collected_incremental = Collector(openalex, raw).collect(incremental)
    normalize_second = OpenAlexNormalizer(raw, prepared).run()

    # A repository renamed on GitHub after an earlier run: that run's row
    # kept the old id and URL, so only the numeric id can tie it to the row
    # the new name produced. Seeded here, folded by the run below.
    prepared.write_models("repositories", [*after_first.repositories.values(), Repository(
        id=STALE_REPO_ID, name="legacy-name", url=STALE_REPO_URL,
        github_id=repo_github_id("BenchOrg7", "AlphaTool"),
        cited_urls=[STALE_REPO_URL], owner_login="BenchOrg7",
        publication_ids=[STALE_REPO_PUBLICATION],
        processing={"repositories": {"status": "completed", "attempts": 1}},
    )])

    with external_services(universe, github, orcid):
        Enricher(prepared, raw, config).run()
    after_second = prepared_state(prepared)

    load_jsonl_dir(client, config.prepared_dir / GROUP)
    snapshot_first = client.snapshot()
    load_jsonl_dir(client, config.prepared_dir / GROUP)
    snapshot_second = client.snapshot()

    # --- third pass: --force after a hypothetical bug fix ------------------
    # Every completed row is recomputed from the same raw payloads, so the
    # published graph must come out byte-for-byte identical.
    with external_services(universe, github, orcid):
        Enricher(prepared, raw, config).run(force=True)
    load_jsonl_dir(client, config.prepared_dir / GROUP)
    snapshot_after_force = client.snapshot()

    return SimpleNamespace(
        config=config,
        github=github,
        collected_first=collected_first,
        collected_again=collected_again,
        collected_incremental=collected_incremental,
        normalize_first=normalize_first,
        normalize_second=normalize_second,
        after_first=after_first,
        after_first_publish=after_first_publish,
        after_second=after_second,
        candidates_first=candidates_first,
        candidates_final=read_journal(prepared.group_dir / "dedup_candidates.jsonl"),
        publications={p.id: p for p in prepared.read_models("publications", Publication)},
        persons={p.id: p for p in prepared.read_models("persons", Person)},
        repositories={r.id: r for r in prepared.read_models("repositories", Repository)},
        profiles={p.id: p for p in prepared.read_models("github_profiles", GitHubProfile)},
        repo_links={r.publication_id: r for r in prepared.read_models("repo_links", RepoLink)},
        graph=client,
        snapshot_first=snapshot_first,
        snapshot_second=snapshot_second,
        snapshot_after_force=snapshot_after_force,
    )


# --- collect / normalize -------------------------------------------------------

def test_collect_is_idempotent(bench):
    assert bench.collected_first == PERIOD_WORKS
    assert bench.collected_again == 0


def test_later_works_arrive_through_a_works_file(bench):
    assert bench.collected_incremental == len(INCREMENTAL_WORK_IDS)
    for work_id in INCREMENTAL_WORK_IDS:
        assert work_id in bench.publications or work_id in PUBLICATION_MERGES


def test_normalize_counts(bench):
    # Normalization keeps one row per OpenAlex work; duplicate records of one
    # publication are folded later, by the dedup stage.
    assert bench.normalize_first == {"publications": PERIOD_WORKS, "persons": TOTAL_AUTHORS}
    # bench.after_first is read after enrichment, i.e. after the dedup stage
    # folded the split authors away.
    assert set(bench.after_first.persons) == set(AUTHOR_IDS) - set(DEDUP_MERGES)


def test_renormalization_does_not_resurrect_merged_rows(bench):
    # The second normalize re-reads every raw payload, including those of the
    # records the first enrichment folded away: their ids must route to the
    # surviving rows instead of coming back as fresh ones.
    assert bench.normalize_second == {
        "publications": PERIOD_WORKS - FIRST_RUN_PUBLICATION_MERGES + len(INCREMENTAL_WORK_IDS),
        "persons": TOTAL_AUTHORS - len(DEDUP_MERGES),
    }
    assert set(bench.persons) == set(AUTHOR_IDS) - set(DEDUP_MERGES)
    for merged_id in PUBLICATION_MERGES:
        assert merged_id not in bench.publications


def test_renormalization_keeps_enrichment_results(bench):
    # Fields the enrichment stages wrote are not derived from the raw work
    # payload, so a re-normalization has to carry them over untouched.
    w1 = bench.after_second.publications["W7000000001"]
    assert w1.code_url == "https://github.com/BenchOrg1/AlphaTool"
    assert w1.processing["code_links"].attempts == 1
    assert bench.after_second.persons["A5000000014"].orcid == "0000-0002-0000-0014"


def test_flaky_affiliation_does_not_split_identity(bench):
    for i in range(1, 6):
        person = bench.persons[f"A50000000{i:02d}"]
        assert person.is_itmo, f"A{i:02d} must be ITMO despite missing affiliations"
    assert sum(p.is_itmo for p in bench.persons.values()) == 47


def test_duplicate_author_entry_kept_per_position(bench):
    a12 = bench.persons["A5000000012"]
    w18_records = [a for a in a12.authored if a.publication_id == "W7000000018"]
    assert len(w18_records) == 2
    assert {a.position for a in w18_records} == {1, 3}


def test_untitled_and_dateless_work(bench):
    w13 = bench.publications["W7000000013"]
    assert w13.title == "Untitled"
    assert w13.year is None and w13.publication_date is None


def test_corresponding_author_flag_reaches_the_graph(bench):
    props = bench.graph.edge_props("AUTHORED", "A5000000068", "W70000000130")
    assert props["is_corresponding"] is True
    assert bench.graph.edge_props(
        "AUTHORED", "A5000000020", "W70000000130")["is_corresponding"] is False


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


def test_deep_link_into_a_repository_is_the_repository(bench):
    # ".../AlphaTool/tree/main/src" and ".../AlphaTool" are one citation.
    urls = [link.url for link in bench.repo_links["W70000000121"].links]
    assert urls == ["https://github.com/BenchOrg8/AlphaTool"]


def test_owner_page_is_not_a_repository(bench):
    urls = [link.url for link in bench.repo_links["W70000000122"].links]
    assert urls == ["https://github.com/BenchOrg9/beta-kit"]


def test_anchor_and_query_suffixes_are_dropped(bench):
    urls = [link.url for link in bench.repo_links["W70000000123"].links]
    assert urls == ["https://github.com/BenchOrg9/GammaLib",
                    "https://github.com/BenchOrg10/AlphaTool"]


def test_upper_case_host_still_resolves_to_the_repository(bench):
    # The citation keeps the odd casing it was written with, but it must not
    # produce a LinkCandidate next to the repository it names.
    links = bench.repo_links["W70000000136"].links
    assert [link.url for link in links] == ["HTTPS://GitHub.COM/BenchOrg13/EpsilonNet"]
    assert bench.graph.targets_of("MENTIONS_LINK", "W70000000136") == \
        {"Repository": {"github_benchorg13_epsilonnet"}}


# --- repositories -----------------------------------------------------------------

def test_every_cited_repository_is_enriched_once(bench):
    completed = [r for r in bench.repositories.values()
                 if r.processing["repositories"].status == "completed"]
    failed = [r for r in bench.repositories.values()
              if r.processing["repositories"].status == "failed"]
    assert len(completed) == 81       # 80 canonical repos + the orphan payload
    assert len(failed) == len(PHANTOM_URLS) == 4
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
    for rid in ("github_goneorg_vanished-repo", "github_goneorg_never-was",
                "github_benchorg5_typo-name", "github_goneorg_ghost-tool"):
        assert bench.repositories[rid].processing["repositories"].status == "failed"
        assert "404" in bench.repositories[rid].processing["repositories"].error


def test_live_and_dead_urls_in_one_abstract_split_by_target(bench):
    edges = bench.graph.targets_of("MENTIONS_LINK", "W70000000124")
    assert edges == {"Repository": {"github_benchorg10_beta-kit"},
                     "LinkCandidate": {"https://github.com/GoneOrg/ghost-tool"}}


def test_repository_without_an_owner_gets_no_profile(bench):
    repo = bench.repositories[ORPHAN_REPO_ID]
    assert repo.processing["repositories"].status == "completed"
    assert repo.owner_login is None
    # html_url was missing too: the URL it was cited by is all there is.
    assert repo.url == "https://github.com/BenchOrg14/orphan-tool"
    assert bench.graph.targets_of("OWNED_BY", ORPHAN_REPO_ID) == {}


def test_second_enrich_run_leaves_completed_repos_alone(bench):
    for rid, repo in bench.after_second.repositories.items():
        first = bench.after_first.repositories.get(rid)
        if first is None or first.processing["repositories"].status != "completed":
            continue
        assert repo.processing["repositories"].attempts == \
            first.processing["repositories"].attempts, f"{rid} was fetched again"


def test_github_profiles_one_per_owner(bench):
    assert len(bench.profiles) == len(REPO_OWNERS) == 16
    assert {p.login for p in bench.profiles.values()} == set(REPO_OWNERS)


# --- transient failures ------------------------------------------------------------

def test_rate_limited_repository_is_retried_on_the_next_run(bench):
    failed = bench.after_first.repositories[FLAKY_REPO_ID].processing["repositories"]
    assert failed.status == "failed" and "429" in failed.error
    healed = bench.after_second.repositories[FLAKY_REPO_ID].processing["repositories"]
    assert healed.status == "completed" and healed.attempts == 2


def test_a_candidate_published_during_an_outage_is_promoted_later(bench):
    # First publish: the repository was never enriched, so the publication
    # only got a LinkCandidate — a stub Repository node would have been a lie.
    assert FLAKY_REPO_ID not in bench.after_first_publish.repositories
    assert FLAKY_REPO_URL in bench.after_first_publish.candidates
    assert ("W70000000126", FLAKY_REPO_URL) in bench.after_first_publish.mentions
    # Second publish, after the retry succeeded: the candidate is gone and the
    # edge hangs off the repository, keeping the properties it carried.
    assert FLAKY_REPO_URL not in bench.graph.nodes["LinkCandidate"]
    assert bench.graph.targets_of("MENTIONS_LINK", "W70000000126") == \
        {"Repository": {FLAKY_REPO_ID}}
    assert bench.graph.edge_props("MENTIONS_LINK", "W70000000126", FLAKY_REPO_ID)[
        "llm_reason"] == "github_url_in_abstract"


def test_failing_orcid_record_is_retried_on_the_next_run(bench):
    first = bench.after_first.persons["A5000000069"]
    assert first.processing["persons"].status == "failed"
    assert "503" in first.processing["persons"].error
    # The ORCID itself came from the author payload, before the record call
    # failed, so only the data behind the record is missing.
    assert first.orcid == SOKOLOV_ORCID and first.email is None
    healed = bench.after_second.persons["A5000000069"]
    assert healed.processing["persons"].status == "completed"
    assert healed.processing["persons"].attempts == 2
    assert healed.email == "t69@example.org"


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
        "W70000000103", "W70000000104", "W70000000105", "W70000000110",
    }
    assert {"E. Smirnova", "Екатерина Смирнова"} <= set(canonical.name_variants)


def test_same_name_without_distinguishing_marks_merges_by_default(bench):
    assert "A5000000057" not in bench.persons
    canonical = bench.persons["A5000000056"]
    assert canonical.merged_ids == ["A5000000057"]
    assert {a.publication_id for a in canonical.authored} == {"W70000000106", "W70000000107"}


def test_conflicting_orcids_stay_separate_and_unreported(bench):
    assert "A5000000058" in bench.persons and "A5000000059" in bench.persons
    pairs = {frozenset((row["person_a"], row["person_b"]))
             for row in bench.candidates_final if "person_a" in row}
    assert frozenset(("A5000000058", "A5000000059")) not in pairs


def test_every_applied_merge_is_journalled_with_its_rule(bench):
    # The merges happen on the first run, so the journal of that run is the
    # audit trail; later runs find nothing left to merge.
    applied = {row["person_a"]: row for row in bench.candidates_first
               if row["status"] == "merged"}
    assert set(applied) == set(DEDUP_MERGES)
    for duplicate, canonical in DEDUP_MERGES.items():
        assert applied[duplicate]["merged_into"] == canonical
    assert applied["A5000000052"]["rules"] == ["orcid"]
    assert applied["A5000000054"]["rules"] == ["name_variant"]
    assert applied["A5000000057"]["rules"] == ["same_name"]
    assert all(row["status"] == "held" for row in bench.candidates_final)


def test_one_orcid_stamped_on_two_namesakes_never_merges_them(bench):
    # The Crossref backfill matched by family name alone and gave both "Li"s
    # the same ORCID...
    assert bench.persons["A5000000060"].orcid == NAMESAKE_ORCID
    assert bench.persons["A5000000061"].orcid == NAMESAKE_ORCID
    # ...but their own OpenAlex records know no ORCID, and that is the
    # trusted source, so the poisoned value is not evidence of anything.
    assert bench.persons["A5000000060"].merged_ids == []
    assert bench.persons["A5000000061"].merged_ids == []


def test_a_group_spanning_two_orcids_is_refused_whole(bench):
    for author_id in ("A5000000062", "A5000000063", "A5000000064"):
        assert author_id in bench.persons, "a bridged group must not be merged"
    held = [row for row in bench.candidates_final
            if row.get("persons") == ["A5000000062", "A5000000063", "A5000000064"]]
    assert len(held) == 1
    assert held[0]["held_because"] == ["group spans 2 distinct ORCID values"]


def test_single_token_namesakes_are_held_not_merged(bench):
    assert "A5000000065" in bench.persons and "A5000000066" in bench.persons
    held = [row for row in bench.candidates_final
            if {row.get("person_a"), row.get("person_b")} == {"A5000000065", "A5000000066"}]
    assert len(held) == 1
    assert "single-token display name" in held[0]["held_because"]


# --- publication dedup -------------------------------------------------------------------

def test_one_doi_re_indexed_twice_is_one_publication(bench):
    assert "W70000000112" not in bench.publications
    survivor = bench.publications["W70000000111"]
    assert survivor.merged_ids == ["W70000000112"]
    assert {v.openalex_id for v in survivor.versions} == {"W70000000111", "W70000000112"}


def test_preprint_folds_into_version_of_record_keeping_both_venues(bench):
    assert "W70000000113" not in bench.publications
    survivor = bench.publications["W70000000114"]
    assert (survivor.journal, survivor.doi) == ("Synthetic Journal",
                                                "https://doi.org/10.7777/vor.114")
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


def test_one_doi_written_two_ways_is_one_publication(bench):
    # Different resolver prefix, different letter case, different titles:
    # only the DOI says these are the same work, and that is enough.
    assert "W70000000132" not in bench.publications
    survivor = bench.publications["W70000000133"]
    assert survivor.merged_ids == ["W70000000132"]
    assert {v.doi for v in survivor.versions} == {
        "http://dx.doi.org/10.7777/CaseDoi.132",
        "https://doi.org/10.7777/casedoi.132",
    }


def test_records_listing_authors_in_reverse_order_yield_one_edge_each(bench):
    # The two records of this work disagree about author order, so the
    # surviving row can carry an authorship per record — but a person and a
    # publication are still joined by exactly one AUTHORED edge.
    edges = {pair for pair in bench.graph.edge_pairs("AUTHORED") if pair[1] == "W70000000133"}
    assert edges == {("A5000000021", "W70000000133"), ("A5000000022", "W70000000133")}


def test_merged_record_fills_every_gap_of_the_survivor(bench):
    # The preprint carried the full text, the PDF, the grant and the code
    # link; the version of record carried none of them.
    assert "W70000000134" not in bench.publications
    survivor = bench.publications["W70000000135"]
    assert survivor.journal == "Synthetic Journal"          # its own field, kept
    assert survivor.pdf_url == "https://example.org/w134.pdf"
    assert survivor.abstract is not None and "134" in survivor.abstract
    assert [g.grant_id for g in survivor.funding] == ["SSF-134"]
    assert survivor.has_code and survivor.code_url == "https://github.com/BenchOrg12/GammaLib"
    assert bench.graph.targets_of("MENTIONS_LINK", "W70000000135") == \
        {"Repository": {"github_benchorg12_gammalib"}}
    assert bench.repositories["github_benchorg12_gammalib"].publication_ids == ["W70000000135"]


def test_authorships_follow_the_surviving_publication(bench):
    for author_id in ("A5000000024", "A5000000025"):
        works = [a.publication_id for a in bench.persons[author_id].authored
                 if a.publication_id in {"W70000000113", "W70000000114"}]
        # One authorship, not one per merged record.
        assert works == ["W70000000114"]


def test_merged_publications_leave_no_graph_node(bench):
    for merged_id in PUBLICATION_MERGES:
        assert merged_id not in bench.graph.nodes["Publication"]


def test_versions_are_json_text(bench):
    props = bench.graph.nodes["Publication"]["W70000000114"]
    assert isinstance(props["versions"], str)
    assert "Synthetic Preprint Server" in props["versions"]


# --- repository dedup --------------------------------------------------------------------

def test_row_written_before_a_rename_folds_into_the_canonical_repository(bench):
    assert STALE_REPO_ID not in bench.repositories
    survivor = bench.repositories[STALE_REPO_CANONICAL_ID]
    assert survivor.merged_ids == [STALE_REPO_ID]
    assert survivor.url == "https://github.com/BenchOrg7/AlphaTool"
    # Both names stay citable, and the publication of the old row survives.
    assert STALE_REPO_URL in survivor.cited_urls
    assert STALE_REPO_PUBLICATION in survivor.publication_ids


# --- departments -----------------------------------------------------------------------

def test_department_matching_including_aliases(bench):
    assert bench.persons["A5000000017"].department_ids == [dept_id("Institute of Applied Computer Science")]
    assert bench.persons["A5000000018"].department_ids == [dept_id("Institute of Applied Computer Science")]
    assert bench.persons["A5000000023"].department_ids == [dept_id("Biotech Research Center")]
    assert bench.persons["A5000000001"].department_ids == []
    assert bench.persons["A5000000031"].department_ids == []


def test_one_person_can_belong_to_two_departments(bench):
    photonics, quantum = dept_id("Faculty of Photonics"), dept_id("Quantum Computing Lab")
    assert set(bench.persons["A5000000067"].department_ids) == {photonics, quantum}
    # A person's departments attach to every publication they authored: the
    # graph says "this work came out of these departments" per author, not
    # per affiliation string of that particular work.
    for work_id in ("W70000000128", "W70000000129"):
        assert {photonics, quantum} <= set(bench.publications[work_id].department_ids)


def test_a_work_collected_later_still_gets_its_authors_departments(bench):
    # W136..W138 arrived after their authors had already been through the
    # departments stage. The stage keeps its state per person, so a skipped
    # person must still attach their departments to the new publications —
    # asserted on the state before the forced run, which would hide it.
    author = bench.after_second.persons["A5000000021"]
    assert author.processing["departments"].attempts == 1, "the author was reprocessed"
    assert author.department_ids, "sanity: this author has a department at all"
    for work_id in ("W70000000136", "W70000000137", "W70000000138"):
        assert set(bench.after_second.publications[work_id].department_ids) >= \
            set(author.department_ids)


def test_russian_only_affiliation_finds_no_department(bench):
    # Known limitation: the catalogue is matched on name_en and the aliases,
    # so a Russian-only affiliation string leaves an ITMO author unattached.
    person = bench.persons["A5000000070"]
    assert person.is_itmo and person.department_ids == []


# --- graph load --------------------------------------------------------------------------

def test_graph_node_counts(bench):
    graph = bench.graph
    assert len(graph.nodes["Publication"]) == TOTAL_WORKS - len(PUBLICATION_MERGES) == 133
    assert len(graph.nodes["Person"]) == TOTAL_AUTHORS - len(DEDUP_MERGES) == 67
    assert len(graph.nodes["Repository"]) == 81
    assert len(graph.nodes["GitHubProfile"]) == 16
    assert len(graph.nodes["LinkCandidate"]) == len(PHANTOM_URLS)
    labels = list(graph.person_labels.values())
    assert labels.count("Itmo") == 47 and labels.count("External") == 20
    for merged_id in DEDUP_MERGES:
        assert merged_id not in graph.nodes["Person"]


def test_link_candidates_are_exactly_the_phantoms(bench):
    assert set(bench.graph.nodes["LinkCandidate"]) == set(PHANTOM_URLS)


def test_one_candidate_node_serves_every_publication_citing_it(bench):
    assert {("W7000000019", PHANTOM_2), ("W70000000138", PHANTOM_2)} <= \
        bench.graph.edge_pairs("MENTIONS_LINK")


def test_authored_edges_match_the_source_payloads(bench):
    # Checked against the OpenAlex payloads plus the merges the dedup stage
    # is expected to apply — not against what the pipeline itself produced.
    assert bench.graph.edge_pairs("AUTHORED") == expected_authorship_pairs()
    assert bench.graph.edge_pairs("AUTHORED") == {
        (person.id, a.publication_id)
        for person in bench.persons.values() for a in person.authored
    }


def test_every_extracted_link_becomes_exactly_one_edge(bench):
    # Counted per publication rather than compared by URL: a link citing a
    # repository by its pre-rename name legitimately ends up on an edge that
    # carries the canonical URL instead (W006).
    published = Counter(src for src, _ in bench.graph.edge_pairs("MENTIONS_LINK"))
    extracted = Counter({row.publication_id: len(row.links)
                         for row in bench.repo_links.values() if row.links})
    assert published == extracted


def test_graph_edge_counts(bench):
    graph = bench.graph
    loaded_repos = [r for r in bench.repositories.values()
                    if r.processing["repositories"].status == "completed"]
    assert graph.edge_pairs("IMPLEMENTS") == {
        (repo.id, publication_id)
        for repo in loaded_repos for publication_id in repo.publication_ids}
    assert graph.edge_pairs("OWNED_BY") == {
        (repo.id, f"github_{repo.owner_login.lower()}")
        for repo in loaded_repos if repo.owner_login}
    assert len(graph.edge_pairs("OWNED_BY")) == 80  # every repo but the orphan
    assert graph.edge_pairs("BELONGS_TO") == {
        (person.id, department_id)
        for person in bench.persons.values() for department_id in person.department_ids}
    assert len(graph.edge_pairs("BELONGS_TO")) == 16  # A17..A30, plus A67 twice
    assert graph.edge_pairs("PRODUCED_BY") == {
        (publication.id, department_id)
        for publication in bench.publications.values()
        for department_id in publication.department_ids}


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


def test_forced_reenrichment_publishes_the_same_graph(bench):
    # --force recomputes every completed row from the same raw payloads; the
    # only thing that may change is the attempt counters, which never reach
    # the graph.
    assert bench.snapshot_after_force == bench.snapshot_second


def test_forced_reenrichment_fetches_each_repository_once_per_run(bench):
    attempts = [call for call in bench.github.calls
                if (call[0].lower(), call[1].lower()) == ("benchorg11", "alphatool")]
    # first run (429), second run (retry), forced run — never twice in one.
    assert len(attempts) == 3
