import json
import tempfile
import unittest
from pathlib import Path

from pauk.graph.dedup import (
    dedup_graph_persons,
    dedup_graph_publications,
    dedup_graph_repositories,
)
from pauk.graph.jsonl_loader import load_jsonl_dir
from pauk.models import Person, Publication, PublicationVersion, RepoLink, Repository
from pauk.pipeline.stages.dedup import CANDIDATES_FILENAME, DedupStage
from pauk.storage import PreparedStore, RawStore
from tests.bench.mocks import RecordingNeo4jClient


def person(pid, name, works, *, itmo=True, orcid=None, variants=(), merged=(),
           email=None, github=None, departments=()):
    return Person(
        id=pid, openalex_id=pid, is_itmo=itmo, name_en=name, orcid=orcid,
        name_variants=list(variants), merged_ids=list(merged),
        email=email, github=github, department_ids=list(departments),
        authored=[{"publication_id": w, "position": 1} for w in works],
    )


def publication(pid, title, *, doi=None, journal=None, day="2026-01-01"):
    return Publication(id=pid, title=title, doi=doi, journal=journal, publication_date=day)


def repository(rid, name, url, *, github_id=None, cited=(), publications=(), day=None):
    return Repository(
        id=rid, name=name, url=url, github_id=github_id,
        cited_urls=list(cited) or [url], publication_ids=list(publications),
        access_date=day,
        processing={"repositories": {"status": "completed", "attempts": 1}},
    )


class DedupStageTest(unittest.TestCase):
    def run_stage(self, people, publications=()):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.prepared = PreparedStore(root / "prepared", "sample")
        self.raw = RawStore(root / "raw", "sample")
        self.prepared.write_models("persons", people)
        self.prepared.write_models("publications", publications)
        result = DedupStage(self.prepared, self.raw).run()
        return result, {p.id: p for p in self.prepared.read_models("persons", Person)}

    def journal(self, status=None):
        path = self.prepared.group_dir / CANDIDATES_FILENAME
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [row for row in rows if status is None or row["status"] == status]

    def test_same_orcid_merges_even_without_name_or_itmo_evidence(self):
        result, people = self.run_stage([
            person("A1", "Maria Petrova", ["W1", "W2"], itmo=False, orcid="0000-0001"),
            person("A2", "Maria Sidorova", ["W3"], itmo=False, orcid="0000-0001"),
        ])
        self.assertEqual(result["dedup_merged"], 1)
        self.assertEqual(set(people), {"A1"})
        merged = people["A1"]
        self.assertEqual(merged.merged_ids, ["A2"])
        self.assertEqual({a.publication_id for a in merged.authored}, {"W1", "W2", "W3"})
        self.assertIn("Maria Sidorova", merged.name_variants)
        self.assertEqual(merged.processing["dedup"].result_count, 1)

    def test_name_variant_with_shared_coauthor_merges(self):
        result, people = self.run_stage([
            person("A1", "Nikolay O. Nikitin", ["W1", "W2"],
                   variants=["Nikolay Nikitin", "N.O. Nikitin"], orcid="0000-0002"),
            person("A2", "Nikolay Nikitin", ["W3"]),
            person("A3", "Ilia Revin", ["W1", "W3"]),
        ])
        self.assertEqual(result["dedup_merged"], 1)
        self.assertEqual(set(people), {"A1", "A3"})
        self.assertEqual(people["A1"].merged_ids, ["A2"])
        self.assertEqual(self.journal("held"), [])
        (applied,) = self.journal("merged")
        self.assertEqual((applied["person_a"], applied["merged_into"], applied["rules"]),
                         ("A2", "A1", ["name_variant"]))

    def test_variant_match_without_shared_coauthor_is_only_a_candidate(self):
        result, people = self.run_stage([
            person("A1", "Nikolay O. Nikitin", ["W1"], variants=["Nikolay Nikitin"]),
            person("A2", "Nikolay Nikitin", ["W3"]),
        ])
        self.assertEqual(result["dedup_merged"], 0)
        self.assertEqual(result["dedup_candidates"], 1)
        self.assertEqual(set(people), {"A1", "A2"})
        (candidate,) = self.journal("held")
        self.assertEqual({candidate["person_a"], candidate["person_b"]}, {"A1", "A2"})
        self.assertEqual(candidate["held_because"], ["no shared coauthors"])

    def test_same_display_name_alone_is_not_enough_to_merge(self):
        # Transliteration collapses distinct Russian names onto one Latin
        # string, so an identical name with nothing behind it is held.
        result, people = self.run_stage([
            person("A1", "Ivan Petrov", ["W1", "W2"]),
            person("A2", "Ivan Petrov", ["W3"]),
        ])
        self.assertEqual((result["dedup_merged"], len(people)), (0, 2))
        (held,) = self.journal("held")
        self.assertEqual(held["held_because"], ["identical name with nothing corroborating it"])

    def test_same_display_name_merges_once_a_shared_field_corroborates_it(self):
        publications = [publication("W1", "One"), publication("W3", "Three")]
        publications[0].fields = ["Computer Science"]
        publications[1].fields = ["Computer Science", "Engineering"]
        result, people = self.run_stage(
            [person("A1", "Ivan Petrov", ["W1", "W2"]), person("A2", "Ivan Petrov", ["W3"])],
            publications=publications,
        )
        self.assertEqual(result["dedup_merged"], 1)
        self.assertEqual(people["A1"].merged_ids, ["A2"])
        (applied,) = self.journal("merged")
        self.assertEqual((applied["person_a"], applied["merged_into"], applied["rules"]),
                         ("A2", "A1", ["same_name"]))

    def test_same_display_name_merges_on_a_shared_department(self):
        result, people = self.run_stage([
            person("A1", "Ivan Petrov", ["W1"], departments=["dept_x"]),
            person("A2", "Ivan Petrov", ["W2"], departments=["dept_x"]),
        ])
        self.assertEqual(result["dedup_merged"], 1)

    def test_a_name_given_as_initials_never_merges_on_the_name_alone(self):
        # "I. V. Smirnov" stands for a range of first names; a shared field
        # is not enough to tell one Smirnov from another.
        publications = [publication("W1", "One"), publication("W2", "Two")]
        for row in publications:
            row.fields = ["Chemistry"]
        result, people = self.run_stage(
            [person("A1", "I. V. Smirnov", ["W1"]), person("A2", "I. V. Smirnov", ["W2"])],
            publications=publications,
        )
        self.assertEqual((result["dedup_merged"], len(people)), (0, 2))
        (held,) = self.journal("held")
        self.assertEqual(held["held_because"], ["name is given as initials"])

    def test_same_name_with_different_orcids_stays_separate(self):
        result, people = self.run_stage([
            person("A1", "Ivan Petrov", ["W1"], orcid="0000-0001"),
            person("A2", "Ivan Petrov", ["W2"], orcid="0000-0002"),
        ])
        self.assertEqual((result["dedup_merged"], result["dedup_candidates"]), (0, 0))
        self.assertEqual(len(people), 2)

    def test_same_name_with_different_emails_stays_separate(self):
        result, people = self.run_stage([
            person("A1", "Ivan Petrov", ["W1"], email="one@itmo.ru"),
            person("A2", "Ivan Petrov", ["W2"], email="two@itmo.ru"),
        ])
        self.assertEqual((result["dedup_merged"], result["dedup_candidates"]), (0, 0))
        self.assertEqual(len(people), 2)

    def test_same_name_with_external_half_is_only_a_candidate(self):
        result, _ = self.run_stage([
            person("A1", "Ivan Petrov", ["W1"]),
            person("A2", "Ivan Petrov", ["W2"], itmo=False),
        ])
        self.assertEqual((result["dedup_merged"], result["dedup_candidates"]), (0, 1))
        (candidate,) = self.journal("held")
        self.assertEqual(candidate["held_because"], ["only one person is ITMO-affiliated"])

    def test_single_token_name_is_only_a_candidate(self):
        result, _ = self.run_stage([
            person("A1", "Ivanov", ["W1"]),
            person("A2", "Ivanov", ["W2"]),
        ])
        self.assertEqual((result["dedup_merged"], result["dedup_candidates"]), (0, 1))
        (candidate,) = self.journal("held")
        self.assertEqual(candidate["held_because"], ["single-token display name"])

    def test_conflicting_orcids_block_merge_and_candidates(self):
        result, people = self.run_stage([
            person("A1", "Nikolay O. Nikitin", ["W1"], variants=["Nikolay Nikitin"], orcid="0000-0002"),
            person("A2", "Nikolay Nikitin", ["W3"], orcid="0000-0003"),
            person("A3", "Ilia Revin", ["W1", "W3"]),
        ])
        self.assertEqual((result["dedup_merged"], result["dedup_candidates"]), (0, 0))
        self.assertEqual(set(people), {"A1", "A2", "A3"})

    def test_external_pair_is_not_merged_and_not_reported(self):
        result, people = self.run_stage([
            person("A1", "Wei Wang", ["W1"], itmo=False, variants=["W. Wang"]),
            person("A2", "W. Wang", ["W2"], itmo=False),
            person("A3", "Shared Coauthor", ["W1", "W2"], itmo=False),
        ])
        self.assertEqual((result["dedup_merged"], result["dedup_candidates"]), (0, 0))
        self.assertEqual(len(people), 3)

    def test_transitive_orcid_group_folds_into_most_published(self):
        result, people = self.run_stage([
            person("A1", "Name One", ["W1"], orcid="0000-0004"),
            person("A2", "Name Two", ["W2", "W3"], orcid="0000-0004"),
            person("A3", "Name Three", ["W4"], orcid="0000-0004"),
        ])
        self.assertEqual(result["dedup_merged"], 2)
        self.assertEqual(set(people), {"A2"})
        self.assertEqual(sorted(people["A2"].merged_ids), ["A1", "A3"])
        self.assertEqual({a.publication_id for a in people["A2"].authored},
                         {"W1", "W2", "W3", "W4"})

    def test_raw_openalex_orcid_overrides_poisoned_prepared_orcid(self):
        # The crossref backfill can stamp a namesake's ORCID onto the wrong
        # person; the raw OpenAlex author record is the trusted source.
        result, people = self.run_stage([
            person("A1", "Li Li", ["W1"], itmo=False, orcid="0000-0001"),
            person("A2", "Li Li", ["W2"], itmo=False, orcid="0000-0001"),
        ])
        # Without raw records prepared orcids are trusted — they merge...
        self.assertEqual(result["dedup_merged"], 1)
        # ...but with raw author records showing different ORCIDs they must not.
        self.tmp2 = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp2.cleanup)
        root = Path(self.tmp2.name)
        prepared = PreparedStore(root / "prepared", "sample")
        raw = RawStore(root / "raw", "sample")
        prepared.write_models("persons", [
            person("A1", "Li Li", ["W1"], itmo=False, orcid="0000-0001"),
            person("A2", "Li Li", ["W2"], itmo=False, orcid="0000-0001"),
        ])
        raw.append("openalex_authors",
                   {"id": "https://openalex.org/A1", "orcid": "https://orcid.org/0000-0001"},
                   {"author_id": "A1"})
        raw.append("openalex_authors",
                   {"id": "https://openalex.org/A2", "orcid": "https://orcid.org/0000-0002"},
                   {"author_id": "A2"})
        result = DedupStage(prepared, raw).run()
        self.assertEqual(result["dedup_merged"], 0)
        self.assertEqual(len(list(prepared.read_models("persons", Person))), 2)

    def test_transitive_bridge_between_different_orcids_blocks_the_group(self):
        # A and B each legitimately pair with the no-ORCID bridge M, but the
        # resulting group would span two distinct ORCIDs — refuse it whole.
        result, people = self.run_stage([
            person("A1", "Anna Ivanova", ["W1", "W2"], orcid="0000-0001",
                   variants=["A. Ivanova"]),
            person("A2", "A. Ivanova", ["W3"], variants=["Anna Ivanova", "Anna B. Ivanova"]),
            person("A3", "Anna B. Ivanova", ["W4"], orcid="0000-0002"),
            person("A4", "Shared Coauthor", ["W1", "W3", "W4"]),
        ])
        self.assertEqual(result["dedup_merged"], 0)
        self.assertEqual(len(people), 4)
        (group_row,) = self.journal("held")
        self.assertEqual(sorted(group_row["persons"]), ["A1", "A2", "A3"])
        self.assertEqual(group_row["held_because"], ["group spans 2 distinct ORCID values"])

    def test_second_run_is_a_no_op(self):
        _, people = self.run_stage([
            person("A1", "Maria Petrova", ["W1", "W2"], orcid="0000-0001"),
            person("A2", "Maria Sidorova", ["W3"], orcid="0000-0001"),
        ])
        again = DedupStage(self.prepared, self.raw).run()
        self.assertEqual(again["dedup_merged"], 0)
        rows = {p.id: p for p in self.prepared.read_models("persons", Person)}
        self.assertEqual(rows["A1"].merged_ids, ["A2"])


class PublicationDedupTest(unittest.TestCase):
    def run_stage(self, publications, people=(), repositories=(), repo_links=()):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.prepared = PreparedStore(root / "prepared", "sample")
        self.raw = RawStore(root / "raw", "sample")
        self.prepared.write_models("publications", publications)
        self.prepared.write_models("persons", people)
        self.prepared.write_models("repositories", repositories)
        self.prepared.write_models("repo_links", repo_links)
        result = DedupStage(self.prepared, self.raw).run()
        rows = {p.id: p for p in self.prepared.read_models("publications", Publication)}
        return result, rows

    def test_same_doi_under_two_work_ids_is_one_publication(self):
        # OpenAlex re-indexing leaves a second record for one DOI, often
        # without any authors at all — the documented one must survive.
        result, publications = self.run_stage(
            [
                publication("W1", "A study", doi="https://doi.org/10.1/x", journal="Journal"),
                publication("W2", "A study", doi="10.1/X", journal="Journal"),
            ],
            people=[person("A1", "Author One", ["W1"])],
        )
        self.assertEqual(result["dedup_publications_merged"], 1)
        self.assertEqual(set(publications), {"W1"})
        self.assertEqual(publications["W1"].merged_ids, ["W2"])
        self.assertEqual({v.openalex_id for v in publications["W1"].versions}, {"W1", "W2"})

    def test_preprint_and_version_of_record_keep_both_venues(self):
        result, publications = self.run_stage([
            publication("W1", "Deep nets for spiders", doi="10.2/preprint",
                        journal="SSRN Electronic Journal", day="2025-06-01"),
            publication("W2", "Deep nets for spiders", doi="10.3/vor",
                        journal="Sensors", day="2026-03-13"),
        ])
        self.assertEqual(result["dedup_publications_merged"], 1)
        # The later record is the version of record and survives.
        self.assertEqual(set(publications), {"W2"})
        survivor = publications["W2"]
        self.assertEqual(survivor.journal, "Sensors")
        self.assertEqual({(v.journal, v.doi) for v in survivor.versions},
                         {("Sensors", "10.3/vor"), ("SSRN Electronic Journal", "10.2/preprint")})

    def test_versions_keep_each_records_own_authors_and_abstract(self):
        # The ledger must answer "who was on the preprint" even after every
        # authorship is repointed to the surviving record.
        preprint = publication("W1", "One work", doi="10.2/preprint", day="2025-06-01")
        preprint.abstract = "Preprint abstract"
        vor = publication("W2", "One work", doi="10.3/vor", day="2026-03-13")
        vor.abstract = "Version-of-record abstract"
        _, publications = self.run_stage(
            [preprint, vor],
            people=[
                person("A1", "Author One", ["W1", "W2"]),
                person("A2", "Author Two", ["W2"]),
            ],
        )
        survivor = publications["W2"]
        by_record = {v.openalex_id: v for v in survivor.versions}
        self.assertEqual([a.person_id for a in by_record["W1"].authors], ["A1"])
        self.assertEqual([(a.person_id, a.name) for a in by_record["W2"].authors],
                         [("A1", "Author One"), ("A2", "Author Two")])
        self.assertEqual(by_record["W1"].abstract, "Preprint abstract")
        self.assertEqual(by_record["W2"].abstract, "Version-of-record abstract")
        # The graph is drawn from the merged state: every author points at
        # the survivor, regardless of which versions they were listed on.
        people = {p.id: p for p in self.prepared.read_models("persons", Person)}
        self.assertEqual([a.publication_id for a in people["A1"].authored], ["W2"])

    def test_identical_titles_differing_only_in_case_and_spacing_merge(self):
        _, publications = self.run_stage([
            publication("W1", "Bandage: continuous build", day="2026-01-12"),
            publication("W2", "  BANDAGE:   Continuous Build ", day="2026-04-10"),
        ])
        self.assertEqual(set(publications), {"W2"})

    def test_untitled_works_are_never_merged(self):
        _, publications = self.run_stage([
            publication("W1", "Untitled"),
            publication("W2", "Untitled"),
        ])
        self.assertEqual(set(publications), {"W1", "W2"})

    def test_a_ledger_entry_without_authors_is_completed_from_raw(self):
        # Records folded before author lists were versioned left entries with
        # the bibliography only, and no later merge revisits them.
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        prepared = PreparedStore(root / "prepared", "sample")
        raw = RawStore(root / "raw", "sample")
        raw.append("openalex_works", {
            "id": "https://openalex.org/W2", "title": "One work",
            "abstract_inverted_index": {"Older": [0], "abstract": [1]},
            "authorships": [
                {"author": {"id": "https://openalex.org/A1", "display_name": "Author One"}},
                {"author": {"id": "https://openalex.org/A9",
                            "display_name": "Association for Computational Linguistics 2026"}},
            ],
        }, {"work_id": "W2"})
        survivor = publication("W1", "One work", doi="10.1/x")
        survivor.merged_ids = ["W2"]
        survivor.versions = [
            PublicationVersion(openalex_id="W1", doi="10.1/x"),
            PublicationVersion(openalex_id="W2", doi="10.1/y"),
        ]
        prepared.write_models("publications", [survivor])
        prepared.write_models("persons", [person("A1", "Author One", ["W1"])])
        DedupStage(prepared, raw).run()
        (row,) = prepared.read_models("publications", Publication)
        ledger = {v.openalex_id: v for v in row.versions}
        self.assertEqual([a.person_id for a in ledger["W1"].authors], ["A1"])
        # The folded record's own author list is rebuilt from its raw payload,
        # without the organization sitting in an author slot.
        self.assertEqual([a.person_id for a in ledger["W2"].authors], ["A1"])
        self.assertEqual(ledger["W2"].abstract, "Older abstract")

    def test_a_fresher_but_empty_record_does_not_become_the_face(self):
        # A re-indexed duplicate can be newer yet carry one author and a DOI
        # whose PDF is gone; the fuller record must stay canonical.
        _, publications = self.run_stage(
            [
                publication("W1", "One work", doi="10.1/x", day="2026-01-01"),
                publication("W2", "One work", doi="10.1/broken", day="2026-02-01"),
            ],
            people=[
                person("A1", "Author One", ["W1", "W2"]),
                person("A2", "Author Two", ["W1"]),
            ],
        )
        self.assertEqual(set(publications), {"W1"})
        self.assertEqual(publications["W1"].doi, "10.1/x")
        # The empty record's DOI is still on file, in versions.
        self.assertEqual({v.doi for v in publications["W1"].versions}, {"10.1/x", "10.1/broken"})

    def test_references_follow_the_surviving_publication(self):
        _, _ = self.run_stage(
            [
                publication("W1", "One work", doi="10.1/x", day="2026-01-01"),
                publication("W2", "One work", doi="10.1/x", day="2026-02-01"),
            ],
            people=[
                person("A1", "Author One", ["W1", "W2"]),
                person("A2", "Author Two", ["W1"]),
            ],
            repositories=[Repository(id="github_org_repo", name="repo",
                                     url="https://github.com/org/repo",
                                     publication_ids=["W1", "W2"])],
            repo_links=[
                RepoLink(publication_id="W1", links=[{"url": "https://github.com/org/repo"}]),
                RepoLink(publication_id="W2", links=[{"url": "https://github.com/org/other"}]),
            ],
        )
        people = {p.id: p for p in self.prepared.read_models("persons", Person)}
        # W1 carries two authorships to W2's one, so it survives, and the
        # same authorship on both records collapses into one.
        self.assertEqual([a.publication_id for a in people["A1"].authored], ["W1"])
        self.assertEqual([a.publication_id for a in people["A2"].authored], ["W1"])
        (repo_row,) = self.prepared.read_models("repositories", Repository)
        self.assertEqual(repo_row.publication_ids, ["W1"])
        # Both records' link rows fold into one row keyed by the survivor.
        (links,) = self.prepared.read_models("repo_links", RepoLink)
        self.assertEqual(links.publication_id, "W1")
        self.assertEqual({link.url for link in links.links},
                         {"https://github.com/org/repo", "https://github.com/org/other"})

    def test_second_run_is_a_no_op(self):
        self.run_stage([
            publication("W1", "One work", doi="10.1/x", day="2026-01-01"),
            publication("W2", "One work", doi="10.1/x", day="2026-02-01"),
        ])
        again = DedupStage(self.prepared, self.raw).run()
        self.assertEqual(again["dedup_publications_merged"], 0)
        rows = {p.id: p for p in self.prepared.read_models("publications", Publication)}
        self.assertEqual(rows["W2"].merged_ids, ["W1"])
        self.assertEqual(len(rows["W2"].versions), 2)


class RepositoryDedupTest(unittest.TestCase):
    def run_stage(self, repositories):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.prepared = PreparedStore(root / "prepared", "sample")
        self.raw = RawStore(root / "raw", "sample")
        self.prepared.write_models("repositories", repositories)
        result = DedupStage(self.prepared, self.raw).run()
        return result, {r.id: r for r in self.prepared.read_models("repositories", Repository)}

    def test_renamed_repository_folds_and_keeps_every_url(self):
        # The old row was written before the rename; only GitHub's numeric id
        # ties the two rows together.
        result, repositories = self.run_stage([
            repository("github_org_old-name", "old-name", "https://github.com/org/old-name",
                       github_id=42, publications=["W1"], day="2026-01-01"),
            repository("github_org_new-name", "new-name", "https://github.com/org/new-name",
                       github_id=42, publications=["W2"], day="2026-06-01"),
        ])
        self.assertEqual(result["dedup_repositories_merged"], 1)
        self.assertEqual(set(repositories), {"github_org_new-name"})
        survivor = repositories["github_org_new-name"]
        self.assertEqual(survivor.url, "https://github.com/org/new-name")
        self.assertIn("https://github.com/org/old-name", survivor.cited_urls)
        self.assertEqual(survivor.publication_ids, ["W2", "W1"])
        self.assertEqual(survivor.merged_ids, ["github_org_old-name"])

    def test_shared_citation_url_folds_rows_without_github_id(self):
        result, repositories = self.run_stage([
            repository("github_org_alpha", "alpha", "https://github.com/org/alpha",
                       cited=["https://github.com/org/Alpha"], day="2026-01-01"),
            repository("github_org_alpha-renamed", "alpha-renamed",
                       "https://github.com/org/alpha-renamed",
                       cited=["https://github.com/org/alpha"], day="2026-06-01"),
        ])
        self.assertEqual(result["dedup_repositories_merged"], 1)
        self.assertEqual(set(repositories), {"github_org_alpha-renamed"})

    def test_distinct_repositories_are_left_alone(self):
        result, repositories = self.run_stage([
            repository("github_org_alpha", "alpha", "https://github.com/org/alpha", github_id=1),
            repository("github_org_beta", "beta", "https://github.com/org/beta", github_id=2),
        ])
        self.assertEqual(result["dedup_repositories_merged"], 0)
        self.assertEqual(len(repositories), 2)


class LoaderPublicationMergeTest(unittest.TestCase):
    def test_previously_published_duplicate_publication_is_folded(self):
        with tempfile.TemporaryDirectory() as tmp:
            prepared = PreparedStore(Path(tmp) / "prepared", "sample")
            client = RecordingNeo4jClient()
            # First publish: both records of the work are still separate.
            prepared.write_models("publications", [
                publication("W1", "One work", doi="10.1/x"),
                publication("W2", "One work", doi="10.1/x"),
            ])
            prepared.write_models("persons", [person("A1", "Author One", ["W1", "W2"])])
            load_jsonl_dir(client, prepared.group_dir)
            assert ("A1", "W2") in client.edge_pairs("AUTHORED")
            # Second publish after dedup: W2 folded into W1.
            prepared.write_models("publications", [
                Publication(id="W1", title="One work", doi="10.1/x", merged_ids=["W2"]),
            ])
            prepared.write_models("persons", [person("A1", "Author One", ["W1"])])
            load_jsonl_dir(client, prepared.group_dir)
            self.assertNotIn("W2", client.nodes["Publication"])
            # The edge that pointed at the duplicate now points at the survivor.
            self.assertEqual(client.edge_pairs("AUTHORED"), {("A1", "W1")})

    def test_previously_published_duplicate_repository_is_folded(self):
        with tempfile.TemporaryDirectory() as tmp:
            prepared = PreparedStore(Path(tmp) / "prepared", "sample")
            client = RecordingNeo4jClient()
            prepared.write_models("publications", [publication("W1", "One work")])
            prepared.write_models("repositories", [
                repository("github_org_old", "old", "https://github.com/org/old",
                           github_id=7, publications=["W1"]),
            ])
            load_jsonl_dir(client, prepared.group_dir)
            assert ("github_org_old", "W1") in client.edge_pairs("IMPLEMENTS")
            prepared.write_models("repositories", [
                repository("github_org_new", "new", "https://github.com/org/new",
                           github_id=7, cited=["https://github.com/org/new",
                                               "https://github.com/org/old"],
                           publications=["W1"]),
            ])
            rows = list(prepared.read_models("repositories", Repository))
            rows[0].merged_ids = ["github_org_old"]
            prepared.write_models("repositories", rows)
            load_jsonl_dir(client, prepared.group_dir)
            self.assertNotIn("github_org_old", client.nodes["Repository"])
            self.assertEqual(client.edge_pairs("IMPLEMENTS"), {("github_org_new", "W1")})


class GraphDedupTest(unittest.TestCase):
    """Cross-group duplicates are invisible to the per-group stage; the
    graph-wide pass compares Person nodes from every published group."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    @staticmethod
    def paper(pid, title, field="Computer Science"):
        """A publication carrying a research field, so that two persons of
        the same name have something corroborating the match."""
        row = publication(pid, title)
        row.fields = [field]
        return row

    def load_group(self, client, group, publications, people):
        prepared = PreparedStore(self.root / "prepared", group)
        prepared.write_models("publications", publications)
        prepared.write_models("persons", people)
        load_jsonl_dir(client, prepared.group_dir)
        return prepared

    def test_cross_group_namesakes_fold_in_graph(self):
        # One researcher collected in two periods: the same full name, and
        # the shared research field corroborates it.
        client = RecordingNeo4jClient()
        self.load_group(client, "q1", [self.paper("W1", "Paper one")],
                        [person("X1", "Julia Borisova", ["W1"])])
        self.load_group(client, "y2026", [self.paper("W2", "Paper two")],
                        [person("Y1", "Julia Borisova", ["W2"], orcid="0009-0001")])
        removed, report = dedup_graph_persons(client, {})
        self.assertEqual(removed, 1)
        # The record carrying an ORCID wins the canonical spot.
        self.assertNotIn("X1", client.nodes["Person"])
        self.assertEqual(client.nodes["Person"]["Y1"]["merged_ids"], ["X1"])
        self.assertEqual(
            {pair for pair in client.edge_pairs("AUTHORED") if pair[0] in {"X1", "Y1"}},
            {("Y1", "W1"), ("Y1", "W2")})
        (applied,) = [row for row in report if row["status"] == "merged"]
        self.assertEqual((applied["person_a"], applied["merged_into"], applied["rules"]),
                         ("X1", "Y1", ["same_name"]))

    def test_raw_orcids_veto_graph_merges_too(self):
        client = RecordingNeo4jClient()
        self.load_group(client, "q1", [publication("W1", "Paper one")],
                        [person("X1", "Julia Borisova", ["W1"])])
        self.load_group(client, "y2026", [publication("W2", "Paper two")],
                        [person("Y1", "Julia Borisova", ["W2"])])
        removed, _report = dedup_graph_persons(client, {"X1": "0009-0001", "Y1": "0009-0002"})
        self.assertEqual(removed, 0)
        self.assertIn("X1", client.nodes["Person"])
        self.assertIn("Y1", client.nodes["Person"])

    def test_cross_group_publication_records_fold_in_graph(self):
        client = RecordingNeo4jClient()
        self.load_group(client, "q1",
                        [publication("W1", "Same work", doi="10.1/x", day="2026-01-01")],
                        [person("X1", "Author One", ["W1"])])
        self.load_group(client, "y2026",
                        [publication("W2", "Same work", doi="10.1/x", day="2026-05-01")],
                        [person("X1", "Author One", ["W2"])])
        removed, report = dedup_graph_publications(client)
        self.assertEqual(removed, 1)
        # The later record survives; edges follow it.
        self.assertNotIn("W1", client.nodes["Publication"])
        self.assertEqual(client.nodes["Publication"]["W2"]["merged_ids"], ["W1"])
        self.assertEqual(
            {pair for pair in client.edge_pairs("AUTHORED") if pair[0] == "X1"},
            {("X1", "W2")})
        (applied,) = report
        self.assertEqual((applied["entity"], applied["record_a"], applied["merged_into"]),
                         ("publication", "W1", "W2"))
        self.assertEqual(applied["rules"], ["doi"])

    def test_graph_fold_writes_the_folded_records_version_entry(self):
        # A cross-group fold deletes the duplicate node; its venue, abstract
        # and author list must land in the canonical's version ledger first.
        client = RecordingNeo4jClient()
        w1 = publication("W1", "Same work", doi="10.1/x", day="2026-01-01")
        w1.abstract = "First-group abstract"
        self.load_group(client, "q1", [w1], [person("X1", "Author One", ["W1"])])
        self.load_group(client, "y2026",
                        [publication("W2", "Same work", doi="10.1/x", day="2026-05-01")],
                        [person("X1", "Author One", ["W2"]),
                         person("X2", "Author Two", ["W2"])])
        dedup_graph_publications(client)
        ledger = {entry["openalex_id"]: entry
                  for entry in json.loads(client.nodes["Publication"]["W2"]["versions"])}
        self.assertEqual(ledger["W1"]["abstract"], "First-group abstract")
        self.assertEqual([a["person_id"] for a in ledger["W1"]["authors"]], ["X1"])
        self.assertEqual({a["person_id"] for a in ledger["W2"]["authors"]}, {"X1", "X2"})

    def test_cross_group_renamed_repository_folds_in_graph(self):
        client = RecordingNeo4jClient()
        for group, rid, url, day in (
            ("q1", "github_org_old", "https://github.com/org/old", "2026-01-01"),
            ("y2026", "github_org_new", "https://github.com/org/new", "2026-06-01"),
        ):
            prepared = PreparedStore(self.root / "prepared", group)
            prepared.write_models("publications", [publication(f"W_{group}", f"Work {group}")])
            prepared.write_models("repositories", [
                repository(rid, rid.split("_")[-1], url, github_id=42,
                           publications=[f"W_{group}"], day=day)])
            load_jsonl_dir(client, prepared.group_dir)
        removed, report = dedup_graph_repositories(client)
        self.assertEqual(removed, 1)
        self.assertNotIn("github_org_old", client.nodes["Repository"])
        survivor = client.nodes["Repository"]["github_org_new"]
        self.assertEqual(survivor["merged_ids"], ["github_org_old"])
        self.assertIn("https://github.com/org/old", survivor["cited_urls"])
        self.assertEqual(client.edge_pairs("IMPLEMENTS"),
                         {("github_org_new", "W_q1"), ("github_org_new", "W_y2026")})
        (applied,) = report
        self.assertEqual(applied["rules"], ["github_id"])

    def test_republish_of_old_group_does_not_resurrect_folded_person(self):
        client = RecordingNeo4jClient()
        old_group = self.load_group(client, "q1", [self.paper("W1", "Paper one")],
                                    [person("X1", "Julia Borisova", ["W1"])])
        self.load_group(client, "y2026", [self.paper("W2", "Paper two")],
                        [person("Y1", "Julia Borisova", ["W2"], orcid="0009-0001")])
        dedup_graph_persons(client, {})
        # The old group's prepared rows still carry X1: republishing them
        # must fold it right back instead of splitting the person again.
        load_jsonl_dir(client, old_group.group_dir)
        self.assertNotIn("X1", client.nodes["Person"])
        self.assertIn(("Y1", "W1"), client.edge_pairs("AUTHORED"))


class FoldPropertyPreservationTest(unittest.TestCase):
    """Folding a duplicate node must not lose what only it knew."""

    def test_edge_attributes_survive_when_the_canonical_edge_already_exists(self):
        # A1 -AUTHORED{affiliation}-> W1 is folded into A2, which already has
        # A2 -AUTHORED{position}-> W1: the surviving edge needs both.
        client = RecordingNeo4jClient()
        client.upsert_nodes_batch("Publication", [("W1", {})])
        client.upsert_person_nodes_batch([("A1", {}), ("A2", {})], is_itmo=True)
        client.upsert_relationships_batch("Person", "Publication", "AUTHORED",
                                          [("A1", "W1", {"affiliation": "ITMO"})])
        client.upsert_relationships_batch("Person", "Publication", "AUTHORED",
                                          [("A2", "W1", {"position": 1})])
        client.merge_person_nodes_batch([("A1", "A2")])
        (props,) = [props for key, props in client.edges.items()
                    if key[3] == "A2" and key[4] == "W1"]
        self.assertEqual(props, {"position": 1, "affiliation": "ITMO"})

    def test_canonical_edge_values_win_over_the_duplicates(self):
        client = RecordingNeo4jClient()
        client.upsert_nodes_batch("Publication", [("W1", {})])
        client.upsert_person_nodes_batch([("A1", {}), ("A2", {})], is_itmo=True)
        client.upsert_relationships_batch("Person", "Publication", "AUTHORED",
                                          [("A1", "W1", {"position": 5, "affiliation": "Old"})])
        client.upsert_relationships_batch("Person", "Publication", "AUTHORED",
                                          [("A2", "W1", {"position": 1})])
        client.merge_person_nodes_batch([("A1", "A2")])
        (props,) = [props for key, props in client.edges.items()
                    if key[3] == "A2" and key[4] == "W1"]
        self.assertEqual(props["position"], 1)
        self.assertEqual(props["affiliation"], "Old")

    def test_node_properties_only_the_duplicate_knew_move_over(self):
        # Folding in the graph (cross-group dedup) deletes the duplicate
        # node; a pdf_url only it carried must reach the canonical first.
        client = RecordingNeo4jClient()
        client.upsert_nodes_batch("Publication", [
            ("W1", {"title": "One work", "doi": "10.1/x"}),
            ("W2", {"title": "One work", "doi": "10.1/y",
                    "pdf_url": "https://example.org/w2.pdf"}),
        ])
        client.merge_publication_nodes_batch([("W2", "W1")])
        self.assertNotIn("W2", client.nodes["Publication"])
        survivor = client.nodes["Publication"]["W1"]
        self.assertEqual(survivor["pdf_url"], "https://example.org/w2.pdf")
        self.assertEqual(survivor["doi"], "10.1/x")  # canonical's own value wins


class LoaderPersonMergeTest(unittest.TestCase):
    def test_previously_published_duplicate_node_is_folded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepared = PreparedStore(root / "prepared", "sample")
            client = RecordingNeo4jClient()
            # First publish: the split still exists.
            prepared.write_models("publications", [])
            prepared.write_models("persons", [
                person("A1", "Nikolay O. Nikitin", ["W1"]),
                person("A2", "Nikolay Nikitin", ["W2"]),
            ])
            client.upsert_nodes_batch("Publication", [("W1", {}), ("W2", {})])
            load_jsonl_dir(client, prepared.group_dir)
            self.assertIn("A2", client.nodes["Person"])
            # Second publish after dedup: A2 folded into A1.
            prepared.write_models("persons", [
                person("A1", "Nikolay O. Nikitin", ["W1", "W2"], merged=["A2"]),
            ])
            load_jsonl_dir(client, prepared.group_dir)
            self.assertNotIn("A2", client.nodes["Person"])
            self.assertEqual(
                {pair for pair in client.edge_pairs("AUTHORED") if pair[0] in {"A1", "A2"}},
                {("A1", "W1"), ("A1", "W2")},
            )


if __name__ == "__main__":
    unittest.main()
