"""Merge prepared rows that describe the same real-world entity.

Three kinds of duplicates reach the prepared layer, each with its own
evidence:

Persons
    OpenAlex author disambiguation sometimes splits one researcher into
    several author records (e.g. "Nikolay Nikitin" and "Nikolay O. Nikitin").
    Person identity in PAUK is the OpenAlex author ID, so each split record
    becomes its own person. Two rules fold them back together:

    1. ORCID: two persons carrying the same ORCID are the same author.
    2. Name variant: one person's display name is listed among the other's
       OpenAlex name variants, both are ITMO-affiliated, and they share at
       least one coauthor.

    Pairs with weaker evidence (an exact display-name match without variant
    confirmation, or a variant match without a shared coauthor) are never
    merged automatically — they are written to dedup_candidates.jsonl in the
    group directory for manual review.

Publications
    One work reaches OpenAlex through several routes: a preprint and the
    version of record, a dataset or software deposit re-released per version,
    and plain duplicate records of one DOI created when OpenAlex re-indexes.
    Rows sharing a DOI, or sharing a title, are one publication. Merging is
    lossless: every record folds into `versions`, so all the places the work
    appeared stay queryable on the surviving row.

Repositories
    A repository renamed or transferred on GitHub keeps its numeric id, so
    rows sharing one are the same repository even though their URLs differ.
    All URLs the repository was ever cited by survive in `cited_urls`.

The stage is deterministic and purely local (no network), so unlike other
stages it re-examines the whole group on every run instead of tracking
per-row processing states; merges already applied simply produce no new
pairs. Merged-away ids are recorded on the surviving row (merged_ids) so
that re-normalization keeps routing the old id to the canonical row and the
graph loader can fold previously published duplicate nodes.
"""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator
from datetime import date, datetime, timezone

from pauk.models import Person, Publication, PublicationVersion, RepoLink, Repository
from pauk.models.processing import ProcessingState, ProcessingStatus
from pauk.pipeline.normalize import _merge_person
from pauk.storage.atomic import AtomicWriter
from pauk.urls import normalize_repo_url

from .base import EnrichmentStage

logger = logging.getLogger(__name__)

CANDIDATES_FILENAME = "dedup_candidates.jsonl"

# normalize.py stores works without a title under this placeholder; it says
# nothing about the work, so it must never be treated as a shared title.
PLACEHOLDER_TITLES = {"", "untitled"}

DOI_PREFIXES = ("https://doi.org/", "http://doi.org/",
                "https://dx.doi.org/", "http://dx.doi.org/", "doi:")


def _norm(name: str | None) -> str:
    return " ".join((name or "").split()).casefold()


def _norm_doi(doi: str | None) -> str | None:
    """Bare lowercase DOI, so a resolver prefix never splits one work."""
    value = _norm(doi)
    for prefix in DOI_PREFIXES:
        value = value.removeprefix(prefix)
    return value.rstrip("/") or None


def _variant_set(person: Person) -> set[str]:
    return {_norm(variant) for variant in person.name_variants if _norm(variant)}


def _grouped(pairs: Iterable[tuple[str, str]]) -> Iterator[list[str]]:
    """Union-find over merge pairs, yielding each group of 2+ members.

    Merging is transitive: A folds into B and B into C means all three are
    one entity even though A and C were never compared directly.
    """
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in pairs:
        parent[find(a)] = find(b)

    groups: dict[str, list[str]] = {}
    for member in parent:
        groups.setdefault(find(member), []).append(member)
    for members in groups.values():
        if len(members) > 1:
            yield members


def _union(*lists: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for values in lists for value in values))


def _version_of(publication: Publication) -> PublicationVersion:
    return PublicationVersion(
        openalex_id=publication.id,
        doi=publication.doi,
        journal=publication.journal,
        publication_date=publication.publication_date,
        year=publication.year,
        openalex_url=publication.openalex_url,
        pdf_url=publication.pdf_url,
    )


def _merge_versions(*sources: Iterable[PublicationVersion]) -> list[PublicationVersion]:
    merged: dict[str, PublicationVersion] = {}
    for versions in sources:
        for version in versions:
            merged.setdefault(version.openalex_id, version)
    return list(merged.values())


def _merge_publication(base: Publication, extra: Publication) -> Publication:
    """Fold one publication record into the record that survives.

    The surviving row keeps its own identity and bibliographic fields (it
    was chosen as the best-documented record); lists become deduplicated
    unions and scalar gaps are filled from the record being merged away, so
    an abstract or a PDF link present on only one record is never lost.
    """
    base.has_code = base.has_code or extra.has_code
    base.versions = _merge_versions(base.versions, extra.versions, [_version_of(extra)])
    base.merged_ids = _union(base.merged_ids, extra.merged_ids)
    base.department_ids = _union(base.department_ids, extra.department_ids)
    for link in extra.mentions_links:
        if link not in base.mentions_links:
            base.mentions_links.append(link)
    for grant in extra.funding:
        if grant not in base.funding:
            base.funding.append(grant)
    for field in ("code_url", "doi", "journal", "publication_date", "year",
                  "openalex_url", "pdf_url", "abstract"):
        if getattr(base, field) is None:
            setattr(base, field, getattr(extra, field))
    for stage, state in extra.processing.items():
        base.processing.setdefault(stage, state)
    return base


def _merge_repository(base: Repository, extra: Repository) -> Repository:
    """Fold one repository row into the row that survives.

    Every URL the merged row was cited by (and the URL it was stored under,
    which is the repository's name before a rename) moves to cited_urls, so
    links published against the old name still resolve to this repository.
    """
    base.cited_urls = _union(base.cited_urls, [extra.url], extra.cited_urls)
    base.merged_ids = _union(base.merged_ids, extra.merged_ids)
    base.publication_ids = _union(base.publication_ids, extra.publication_ids)
    base.department_ids = _union(base.department_ids, extra.department_ids)
    base.contributors = _union(base.contributors, extra.contributors)
    for field in ("github_id", "description", "access_date", "stars_num",
                  "last_updated", "license", "owner_login"):
        if getattr(base, field) is None:
            setattr(base, field, getattr(extra, field))
    base.has_readme = base.has_readme or extra.has_readme
    for stage, state in extra.processing.items():
        base.processing.setdefault(stage, state)
    return base


class DedupStage(EnrichmentStage):
    name = "dedup"

    def run(self) -> dict[str, int]:
        publication_merges = self._dedup_publications()
        repository_merges = self._dedup_repositories()
        merged_persons, candidates = self._dedup_persons()
        return {
            "dedup_publications_merged": len(publication_merges),
            "dedup_repositories_merged": len(repository_merges),
            "dedup_merged": merged_persons,
            "dedup_candidates": candidates,
        }

    # --- publications ---------------------------------------------------------

    def _dedup_publications(self) -> dict[str, str]:
        """Fold duplicate publication records and repoint every reference.

        Returns a mapping of merged-away publication id to the surviving id.
        """
        publications = list(self.prepared.read_models("publications", Publication))
        if not publications:
            return {}
        by_id = {publication.id: publication for publication in publications}
        in_scope = self._scope("publications", set(by_id))

        # The best-documented record survives, so authorship counts decide
        # between records of one DOI (OpenAlex re-indexing leaves one of them
        # without any authors at all).
        author_counts: Counter[str] = Counter()
        for person in self.prepared.read_models("persons", Person):
            for authorship in person.authored:
                author_counts[authorship.publication_id] += 1

        pairs = self._pairs_by_key(publications, in_scope, (
            lambda publication: _norm_doi(publication.doi),
            lambda publication: (
                title if (title := _norm(publication.title)) not in PLACEHOLDER_TITLES else None
            ),
        ))

        id_map: dict[str, str] = {}
        for members in _grouped(pairs):
            ranked = sorted(
                (by_id[member] for member in members),
                # Latest first: the version of record follows its preprint and
                # the newest deposit supersedes earlier ones. Ties go to the
                # record carrying the most authorships, then to the smallest
                # id so the choice never depends on file order.
                key=lambda publication: (
                    -(publication.publication_date or date.min).toordinal(),
                    -author_counts[publication.id],
                    publication.id,
                ),
            )
            canonical, duplicates = ranked[0], ranked[1:]
            canonical.versions = _merge_versions(canonical.versions, [_version_of(canonical)])
            for duplicate in duplicates:
                logger.info("dedup: merging publication %s (%s) into %s (%s)",
                            duplicate.id, duplicate.journal, canonical.id, canonical.journal)
                _merge_publication(canonical, duplicate)
                canonical.merged_ids = _union(canonical.merged_ids, [duplicate.id])
                id_map[duplicate.id] = canonical.id
            self._record_state(canonical, len(duplicates))

        if not id_map:
            return {}
        self.prepared.write_models(
            "publications", [row for row in publications if row.id not in id_map])
        self._repoint_publication_references(id_map)
        return id_map

    def _repoint_publication_references(self, id_map: dict[str, str]) -> None:
        """Rewrite every prepared row that referenced a merged publication."""
        people = list(self.prepared.read_models("persons", Person))
        for person in people:
            authored = []
            for authorship in person.authored:
                authorship.publication_id = id_map.get(
                    authorship.publication_id, authorship.publication_id)
                # Exact duplicates only: one author legitimately appears
                # twice on one work (different positions), and both records
                # of a merged work list the same authors in the same order.
                if authorship not in authored:
                    authored.append(authorship)
            person.authored = authored
        self.prepared.write_models("persons", people)

        repositories = list(self.prepared.read_models("repositories", Repository))
        for repository in repositories:
            repository.publication_ids = _union(
                id_map.get(pid, pid) for pid in repository.publication_ids)
        self.prepared.write_models("repositories", repositories)

        merged_links: dict[str, RepoLink] = {}
        for row in self.prepared.read_models("repo_links", RepoLink):
            row.publication_id = id_map.get(row.publication_id, row.publication_id)
            existing = merged_links.get(row.publication_id)
            if existing is None:
                merged_links[row.publication_id] = row
                continue
            known = {link.url for link in existing.links}
            existing.links.extend(link for link in row.links if link.url not in known)
        self.prepared.write_models("repo_links", merged_links.values())

    # --- repositories ---------------------------------------------------------

    def _dedup_repositories(self) -> dict[str, str]:
        """Fold repository rows that GitHub considers one repository."""
        repositories = list(self.prepared.read_models("repositories", Repository))
        if not repositories:
            return {}
        by_id = {repository.id: repository for repository in repositories}
        in_scope = self._scope("repositories", set(by_id))

        pairs = self._pairs_by_key(repositories, in_scope, (
            lambda repository: str(repository.github_id) if repository.github_id else None,
            # Safety net for rows written before repositories were re-keyed
            # to their canonical identity: one stored URL, one repository.
            lambda repository: normalize_repo_url(repository.url),
        ))
        # A URL cited on two rows means the repository was renamed between
        # runs: the old row kept the citation, the new row was created from
        # the canonical URL served afterwards.
        by_cited: dict[str, list[Repository]] = defaultdict(list)
        for repository in repositories:
            for cited in repository.cited_urls:
                by_cited[normalize_repo_url(cited)].append(repository)
        pairs.extend(self._bucket_pairs(by_cited.values(), in_scope))

        id_map: dict[str, str] = {}
        for members in _grouped(pairs):
            ranked = sorted(
                (by_id[member] for member in members),
                # An enriched row carries GitHub's current canonical URL; the
                # freshest one of those wins, then the most cited row.
                key=lambda repository: (
                    (repository.processing.get("repositories") or ProcessingState()).status
                    != ProcessingStatus.COMPLETED,
                    -(repository.access_date or date.min).toordinal(),
                    -len(repository.publication_ids),
                    repository.id,
                ),
            )
            canonical, duplicates = ranked[0], ranked[1:]
            for duplicate in duplicates:
                logger.info("dedup: merging repository %s (%s) into %s (%s)",
                            duplicate.id, duplicate.url, canonical.id, canonical.url)
                _merge_repository(canonical, duplicate)
                canonical.merged_ids = _union(canonical.merged_ids, [duplicate.id])
                id_map[duplicate.id] = canonical.id
            self._record_state(canonical, len(duplicates))

        if not id_map:
            return {}
        self.prepared.write_models(
            "repositories", [row for row in repositories if row.id not in id_map])
        people = list(self.prepared.read_models("persons", Person))
        for person in people:
            for contribution in person.contributed_to:
                contribution.repository_id = id_map.get(
                    contribution.repository_id, contribution.repository_id)
        self.prepared.write_models("persons", people)
        return id_map

    # --- persons ---------------------------------------------------------------

    def _dedup_persons(self) -> tuple[int, int]:
        people = list(self.prepared.read_models("persons", Person))
        by_id = {person.id: person for person in people}
        in_scope = self._person_scope(people)
        trusted_orcid = self._trusted_orcids(people)

        pub_authors: dict[str, set[str]] = {}
        for person in people:
            for authorship in person.authored:
                pub_authors.setdefault(authorship.publication_id, set()).add(person.id)

        coauthor_cache: dict[str, set[str]] = {}

        def coauthors(person: Person) -> set[str]:
            cached = coauthor_cache.get(person.id)
            if cached is None:
                cached = set()
                for authorship in person.authored:
                    cached |= pub_authors.get(authorship.publication_id, set())
                cached.discard(person.id)
                coauthor_cache[person.id] = cached
            return cached

        merge_pairs: list[tuple[str, str]] = []
        candidates: list[dict] = []
        seen_pairs: set[tuple[str, str]] = set()

        for first, second in self._paired(people, in_scope):
            key = (first.id, second.id)
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            first_orcid = trusted_orcid.get(first.id)
            second_orcid = trusted_orcid.get(second.id)
            if first_orcid and second_orcid:
                if first_orcid == second_orcid:
                    merge_pairs.append(key)
                # Different ORCIDs are explicit evidence of two distinct
                # people — never merge and not worth reporting either.
                continue

            first_name, second_name = _norm(first.name_en), _norm(second.name_en)
            if not first_name or not second_name:
                continue
            variant_evidence = (
                first_name != second_name
                and (first_name in _variant_set(second) or second_name in _variant_set(first))
            )
            same_name = first_name == second_name
            if not variant_evidence and not same_name:
                continue
            both_itmo = first.is_itmo and second.is_itmo
            shared = (coauthors(first) & coauthors(second)) - {first.id, second.id}

            if variant_evidence and both_itmo and shared:
                merge_pairs.append(key)
            elif first.is_itmo or second.is_itmo:
                reasons = []
                if not variant_evidence:
                    reasons.append("same display name is not confirmed by a name variant")
                if not both_itmo:
                    reasons.append("only one person is ITMO-affiliated")
                if not shared:
                    reasons.append("no shared coauthors")
                candidates.append({
                    "person_a": first.id, "name_a": first.name_en,
                    "person_b": second.id, "name_b": second.name_en,
                    "shared_coauthors": len(shared),
                    "held_because": reasons,
                })

        removed = self._apply_person_merges(by_id, merge_pairs, trusted_orcid)
        if removed:
            people = [person for person in people if person.id not in removed]
            self.prepared.write_models("persons", people)

        candidates_path = self.prepared.group_dir / CANDIDATES_FILENAME
        with AtomicWriter(candidates_path) as fh:
            for row in candidates:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        if candidates:
            logger.info("dedup: %d candidate pair(s) left for manual review in %s",
                        len(candidates), candidates_path)
        return len(removed), len(candidates)

    def _trusted_orcids(self, people: list[Person]) -> dict[str, str | None]:
        """ORCID per person id, preferring the raw OpenAlex author record.

        The prepared orcid field is not authoritative: the crossref backfill
        in the persons stage assigns an ORCID by surname match alone, which
        can stamp a namesake's ORCID onto the wrong person (common names
        like "Li Li"). The author's own OpenAlex record is the trusted
        source; where a raw record was fetched it overrides the prepared
        value — including overriding it with None when OpenAlex knows no
        ORCID for that author.
        """
        raw_orcids: dict[str, str | None] = {}
        for envelope in self.raw.read("openalex_authors"):
            payload = envelope.get("payload") or {}
            author_id = (payload.get("id") or "").rstrip("/").rsplit("/", 1)[-1]
            if author_id:
                raw_orcids[author_id] = (payload.get("orcid") or "").rstrip("/").rsplit("/", 1)[-1] or None
        return {
            person.id: raw_orcids[person.id] if person.id in raw_orcids else person.orcid
            for person in people
        }

    def _person_scope(self, people: list[Person]) -> set[str] | None:
        """Person ids the selection allows to participate in merging."""
        if self.selection is None:
            return None
        if self.selection.entity == "persons":
            return set(self.selection.ids)
        if self.selection.entity == "publications":
            return {
                person.id for person in people
                if any(a.publication_id in self.selection.ids for a in person.authored)
            }
        return set()

    def _paired(self, people: list[Person], in_scope: set[str] | None):
        """Yield person pairs worth comparing.

        Blocking keeps this quadratic only within small buckets: name-based
        pairs must share a name token, ORCID pairs are grouped exactly.
        """
        by_orcid: dict[str, list[Person]] = {}
        by_token: dict[str, list[Person]] = {}
        for person in people:
            if person.orcid:
                by_orcid.setdefault(person.orcid, []).append(person)
            for name in (person.name_en, *person.name_variants):
                for token in _norm(name).replace(",", " ").split():
                    if len(token) > 2:
                        by_token.setdefault(token, []).append(person)

        emitted: set[tuple[str, str]] = set()
        for bucket in (*by_orcid.values(), *by_token.values()):
            unique = list({person.id: person for person in bucket}.values())
            for i, first in enumerate(unique):
                for second in unique[i + 1:]:
                    if in_scope is not None and first.id not in in_scope and second.id not in in_scope:
                        continue
                    pair = tuple(sorted((first.id, second.id)))
                    if pair in emitted:
                        continue
                    emitted.add(pair)
                    yield first, second

    def _apply_person_merges(self, by_id: dict[str, Person], pairs: list[tuple[str, str]],
                             trusted_orcid: dict[str, str | None]) -> set[str]:
        """Fold each group of paired persons into a canonical person.

        The canonical person is the one with the most authored works
        (ties: having an ORCID, then the smallest id, for determinism).

        Pairwise checks can't see transitive contradictions: A and B may
        each legitimately pair with a bridge person M yet carry different
        ORCIDs themselves. Any group holding more than one distinct ORCID
        is therefore skipped entirely and left for manual review.
        """
        removed: set[str] = set()
        for members in _grouped(pairs):
            distinct_orcids = {trusted_orcid.get(member) for member in members} - {None}
            if len(distinct_orcids) > 1:
                logger.warning(
                    "dedup: refusing to merge group %s — it spans %d distinct ORCIDs",
                    sorted(members), len(distinct_orcids))
                continue
            ranked = sorted(
                (by_id[member] for member in members),
                key=lambda p: (-len(p.authored), p.orcid is None, p.id),
            )
            canonical, duplicates = ranked[0], ranked[1:]
            for duplicate in duplicates:
                logger.info("dedup: merging %s (%s) into %s (%s)",
                            duplicate.id, duplicate.name_en, canonical.id, canonical.name_en)
                _merge_person(canonical, duplicate)
                if duplicate.name_en:
                    canonical.name_variants = _union(canonical.name_variants, [duplicate.name_en])
                canonical.merged_ids = _union(canonical.merged_ids, [duplicate.id])
                removed.add(duplicate.id)
            self._record_state(canonical, len(duplicates))
        return removed

    # --- shared helpers ---------------------------------------------------------

    def _scope(self, entity: str, all_ids: set[str]) -> set[str] | None:
        """Ids of `entity` the selection allows to participate in merging."""
        if self.selection is None:
            return None
        return set(self.selection.ids) & all_ids if self.selection.entity == entity else set()

    def _pairs_by_key(self, rows: list, in_scope: set[str] | None,
                      key_functions: tuple[Callable, ...]) -> list[tuple[str, str]]:
        """Pair rows that share a key under any of the given key functions."""
        pairs: list[tuple[str, str]] = []
        for key_of in key_functions:
            buckets: dict[str, list] = defaultdict(list)
            for row in rows:
                key = key_of(row)
                if key:
                    buckets[key].append(row)
            pairs.extend(self._bucket_pairs(buckets.values(), in_scope))
        return pairs

    @staticmethod
    def _bucket_pairs(buckets: Iterable[list], in_scope: set[str] | None) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        for bucket in buckets:
            unique = list({row.id: row for row in bucket})
            for other in unique[1:]:
                if in_scope is not None and unique[0] not in in_scope and other not in in_scope:
                    continue
                pairs.append((unique[0], other))
        return pairs

    def _record_state(self, row, merged_count: int) -> None:
        state = row.processing.get(self.name)
        row.processing[self.name] = ProcessingState(
            status=ProcessingStatus.COMPLETED,
            attempts=(state.attempts if state else 0) + 1,
            finished_at=datetime.now(timezone.utc),
            result_count=merged_count,
        )
