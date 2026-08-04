"""Merge prepared rows that describe the same real-world entity.

Three kinds of duplicates reach the prepared layer, each with its own
evidence:

Persons
    OpenAlex author disambiguation sometimes splits one researcher into
    several author records (e.g. "Nikolay Nikitin" and "Nikolay O. Nikitin").
    Person identity in PAUK is the OpenAlex author ID, so each split record
    becomes its own person. Three rules fold them back together:

    1. ORCID: two persons carrying the same ORCID are the same author.
    2. Name variant: one person's display name is listed among the other's
       OpenAlex name variants, both are ITMO-affiliated, and they share at
       least one coauthor.
    3. Identical name plus corroboration: two ITMO-affiliated persons whose
       full display names match exactly and who also share a coauthor, a
       department or a research field.

    Merge policy
    ------------
    A name is evidence, never proof. Transliteration collapses distinct
    Russian names onto one Latin string, and the most common surnames
    (Smirnov, Novikov, Ivanov) collide inside a single university's author
    pool — an audit of merging on the name alone found "I. V. Smirnov" the
    medical-anthropology reviewer folded into "I. V. Smirnov" the chemist.
    So rule 3 requires an independent signal, and a name given as initials
    ("I. V. Smirnov") is never enough on its own: initials stand for a whole
    range of first names, multiplying the collisions a full name would not
    produce.

    An explicitly different identity field (ORCID, email, GitHub login,
    OpenReview id, Google Scholar id) always keeps a pair separate. Pairs
    with weaker evidence — a variant match without a shared coauthor, a
    namesake outside ITMO, a single-token name, an identical name with
    nothing corroborating it — are never merged automatically; they land in
    the review journal with the reason, and a person who really was split
    stays split until someone confirms it.

    TODO: the corroboration signals are still coarse. A shared field
    ("Computer Science") is weak on its own, a shared department is only as
    good as the affiliation strings behind it, and neither says anything
    about two namesakes in the same lab. Worth exploring: publication-year
    ranges that cannot belong to one career, coauthor-graph distance rather
    than a plain intersection, and per-rule precision measured against the
    decisions reviewers make in the journal.

    Every heuristic decision is journalled to dedup_candidates.jsonl in the
    group directory: applied merges carry status "merged" with the rule(s)
    that justified them — so they can be audited and rolled back — and
    held-back pairs and groups carry status "held" with the reasons.

    The same rules serve the graph-wide pass (pauk dedup graph, see
    pauk/graph/dedup.py), which compares persons across all published
    groups — duplicates whose records live in different groups are
    invisible to this per-group stage.

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
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator
from datetime import date, datetime, timezone

from pauk.models import Person, Publication, PublicationVersion, RepoLink, Repository, VersionAuthor
from pauk.models.processing import ProcessingState, ProcessingStatus
from pauk.pipeline.normalize import (
    ORG_AUTHOR_NAME,
    _abstract,
    _fallback_person_id,
    _merge_person,
    _short_id,
)
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


def _publication_rank_key(publication_type: str | None, publication_date_ordinal: int,
                          author_count: int, publication_id: str) -> tuple[int, int, int, str]:
    """Sort key for the representative record of one work.

    A journal article is the preferred face of a work and a preprint the
    least preferred one. Other OpenAlex work types (and a missing type) are
    deliberately equal between those two extremes. Within a status, newer
    records win, then the record with more authors; the id makes ties stable.
    """
    status_rank = {"article": 0, "preprint": 2}.get((publication_type or "").casefold(), 1)
    return status_rank, -publication_date_ordinal, -author_count, publication_id


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


# Identity fields a person can carry explicitly; two persons differing in any
# of them are two real people no matter how similar their names are.
PROFILE_FIELDS = ("email", "github", "openreview", "google_scholar")


def _profiles_conflict(first: Person, second: Person) -> bool:
    return any(
        getattr(first, field) and getattr(second, field)
        and getattr(first, field) != getattr(second, field)
        for field in PROFILE_FIELDS
    )


def _paired_persons(people: list[Person], in_scope: set[str] | None):
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


# Names given as initials ("I. V. Smirnov") stand for a whole range of first
# names, so an identical one says far less than an identical full name.
INITIALS_NAME = re.compile(r"^(?:\w\.\s*){1,3}\S")


def _is_initials_name(name: str) -> bool:
    return bool(INITIALS_NAME.match(name.strip()))


def plan_person_merges(
    people: list[Person],
    trusted_orcid: dict[str, str | None],
    in_scope: set[str] | None = None,
    fields_of: dict[str, set[str]] | None = None,
) -> tuple[list[tuple[Person, list[Person]]], list[dict]]:
    """Decide which persons are one author and which pairs need human eyes.

    Shared by the per-group dedup stage and the graph-wide pass (pauk
    dedup graph): both feed Person rows in, only the storage they apply the
    result to differs.

    Returns:
        (groups, report): groups as (canonical, duplicates) tuples — the
        canonical person is the one with the most authored works (ties:
        having an ORCID, then the smallest id) — and review-journal rows.
        Every heuristic decision lands in the journal: applied merges carry
        status "merged" plus the rule(s) that justified them (so they can
        be audited and rolled back via merged_ids), pairs and groups held
        back carry status "held" plus the reasons.
    """
    by_id = {person.id: person for person in people}
    fields_of = fields_of or {}

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

    def research_fields(person: Person) -> set[str]:
        return {
            field for authorship in person.authored
            for field in fields_of.get(authorship.publication_id, ())
        }

    merge_pairs: list[tuple[str, str]] = []
    pair_rules: dict[frozenset, str] = {}
    report: list[dict] = []

    def plan_pair(first: Person, second: Person, rule: str) -> None:
        merge_pairs.append((first.id, second.id))
        pair_rules[frozenset((first.id, second.id))] = rule

    for first, second in _paired_persons(people, in_scope):
        first_orcid = trusted_orcid.get(first.id)
        second_orcid = trusted_orcid.get(second.id)
        if first_orcid and second_orcid:
            if first_orcid == second_orcid:
                plan_pair(first, second, "orcid")
            # Different ORCIDs are explicit evidence of two distinct
            # people — never merge and not worth reporting either.
            continue
        if _profiles_conflict(first, second):
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
        multi_token = len(first_name.split()) > 1
        shared = (coauthors(first) & coauthors(second)) - {first.id, second.id}
        shared_departments = set(first.department_ids) & set(second.department_ids)
        shared_fields = research_fields(first) & research_fields(second)
        # A name on its own is never enough — see the merge policy above.
        corroboration = bool(shared or shared_departments or shared_fields)
        initials_only = _is_initials_name(first.name_en or "")

        if variant_evidence and both_itmo and shared:
            plan_pair(first, second, "name_variant")
        elif same_name and both_itmo and multi_token and not initials_only and corroboration:
            plan_pair(first, second, "same_name")
        elif first.is_itmo or second.is_itmo:
            reasons = []
            if not both_itmo:
                reasons.append("only one person is ITMO-affiliated")
            if same_name and both_itmo and not multi_token:
                reasons.append("single-token display name")
            if same_name and both_itmo and multi_token and initials_only:
                reasons.append("name is given as initials")
            if same_name and both_itmo and multi_token and not initials_only and not corroboration:
                reasons.append("identical name with nothing corroborating it")
            if variant_evidence and both_itmo and not shared:
                reasons.append("no shared coauthors")
            report.append({
                "status": "held",
                "person_a": first.id, "name_a": first.name_en,
                "person_b": second.id, "name_b": second.name_en,
                "shared_coauthors": len(shared),
                "shared_departments": len(shared_departments),
                "shared_fields": sorted(shared_fields),
                "held_because": reasons,
            })

    groups: list[tuple[Person, list[Person]]] = []
    for members in _grouped(merge_pairs):
        # Pairwise checks can't see transitive contradictions: A and B may
        # each legitimately pair with a bridge person M yet differ from
        # each other. A group spanning more than one ORCID (or any other
        # explicit identity value) is refused whole, for manual review.
        conflict = _group_conflict(members, by_id, trusted_orcid)
        if conflict:
            field, values = conflict
            logger.warning(
                "dedup: refusing to merge group %s — it spans %d distinct %s values",
                sorted(members), len(values), field)
            report.append({
                "status": "held",
                "persons": sorted(members),
                "names": [by_id[member].name_en for member in sorted(members)],
                "held_because": [f"group spans {len(values)} distinct {field} values"],
            })
            continue
        ranked = sorted(
            (by_id[member] for member in members),
            key=lambda p: (-len(p.authored), p.orcid is None, p.id),
        )
        canonical, duplicates = ranked[0], ranked[1:]
        groups.append((canonical, duplicates))
        for duplicate in duplicates:
            report.append({
                "status": "merged",
                "person_a": duplicate.id, "name_a": duplicate.name_en,
                "person_b": canonical.id, "name_b": canonical.name_en,
                "merged_into": canonical.id,
                "rules": sorted({
                    rule for pair, rule in pair_rules.items() if duplicate.id in pair
                }),
            })
    return groups, report


def _group_conflict(
    members: list[str], by_id: dict[str, Person],
    trusted_orcid: dict[str, str | None],
) -> tuple[str, set[str]] | None:
    """The first identity field whose values split the group, if any."""
    orcids = {trusted_orcid.get(member) for member in members} - {None}
    if len(orcids) > 1:
        return "ORCID", orcids
    for field in PROFILE_FIELDS:
        values = {getattr(by_id[member], field) for member in members} - {None}
        if len(values) > 1:
            return field, values
    return None


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


def _version_of(publication: Publication,
                authors: Iterable[VersionAuthor] = ()) -> PublicationVersion:
    return PublicationVersion(
        openalex_id=publication.id,
        title=publication.title,
        doi=publication.doi,
        journal=publication.journal,
        publication_date=publication.publication_date,
        year=publication.year,
        openalex_url=publication.openalex_url,
        pdf_url=publication.pdf_url,
        abstract=publication.abstract,
        authors=list(authors),
    )


def _merge_versions(*sources: Iterable[PublicationVersion]) -> list[PublicationVersion]:
    """One entry per record. The first source naming a record wins; later
    sources only fill fields it left empty — which is how ledger entries
    written before authors and abstracts were tracked gain them."""
    merged: dict[str, PublicationVersion] = {}
    for versions in sources:
        for version in versions:
            existing = merged.setdefault(version.openalex_id, version)
            if existing is version:
                continue
            for field in ("title", "doi", "journal", "publication_date",
                          "year", "openalex_url", "pdf_url", "abstract"):
                if getattr(existing, field) is None:
                    setattr(existing, field, getattr(version, field))
            if not existing.authors:
                existing.authors = version.authors
    return list(merged.values())


def _merge_publication(base: Publication, extra: Publication,
                       extra_authors: Iterable[VersionAuthor] = ()) -> Publication:
    """Fold one publication record into the record that survives.

    The surviving row keeps its own identity and bibliographic fields (it
    was chosen as the best-documented record); lists become deduplicated
    unions and scalar gaps are filled from the record being merged away, so
    an abstract or a PDF link present on only one record is never lost.
    """
    base.has_code = base.has_code or extra.has_code
    base.versions = _merge_versions(base.versions, extra.versions,
                                    [_version_of(extra, extra_authors)])
    base.merged_ids = _union(base.merged_ids, extra.merged_ids)
    base.department_ids = _union(base.department_ids, extra.department_ids)
    for link in extra.mentions_links:
        if link not in base.mentions_links:
            base.mentions_links.append(link)
    for grant in extra.funding:
        if grant not in base.funding:
            base.funding.append(grant)
    for field in ("type", "code_url", "doi", "journal", "publication_date", "year",
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

    def _refresh_version_ledger(self, publications: list[Publication],
                                version_authors: dict[str, list[VersionAuthor]],
                                known_persons: dict[str, Person]) -> int:
        """Rebuild ledger entries from the raw payload of each record.

        Raw is the only per-record source that stays correct: a ledger entry
        written after a merge would otherwise describe the merged state (the
        union of every record's authors), and entries written before author
        lists were versioned hold nothing at all. Only authors that exist as
        persons here are kept, which leaves out organizations sitting in
        author slots, coauthors dropped by the consortium cap, and ids that
        person dedup has since folded away.
        """
        if not any(publication.versions for publication in publications):
            return 0

        raw_works: dict[str, dict] = {}
        for envelope in self.raw.read("openalex_works"):
            payload = envelope.get("payload") or {}
            work_id = _short_id(payload.get("id"))
            if work_id:
                raw_works[work_id] = payload

        refreshed = 0
        for publication in publications:
            for version in publication.versions:
                work = raw_works.get(version.openalex_id)
                if work is None:
                    # No raw payload (an older group, another machine): keep
                    # whatever the entry already carries.
                    if not version.authors and version.openalex_id == publication.id:
                        version.authors = list(version_authors.get(publication.id, ()))
                        refreshed += bool(version.authors)
                    continue
                authors = []
                for position, authorship in enumerate(work.get("authorships") or [], start=1):
                    author = authorship.get("author") or {}
                    if ORG_AUTHOR_NAME.search(author.get("display_name") or ""):
                        continue
                    person_id = _short_id(author.get("id")) or _fallback_person_id(author)
                    person = known_persons.get(person_id) if person_id else None
                    if person is None:
                        continue
                    authors.append(VersionAuthor(person_id=person.id, name=person.name_en,
                                                 position=position))
                if version.abstract is None:
                    version.abstract = _abstract(work)
                if authors != version.authors:
                    version.authors = authors
                    refreshed += 1
        if refreshed:
            logger.info("dedup: rebuilt %d version-ledger entry(ies) from raw payloads", refreshed)
        return refreshed

    def _dedup_publications(self) -> dict[str, str]:
        """Fold duplicate publication records and repoint every reference.

        Returns a mapping of merged-away publication id to the surviving id.
        """
        publications = list(self.prepared.read_models("publications", Publication))
        if not publications:
            return {}
        by_id = {publication.id: publication for publication in publications}
        in_scope = self._scope("publications", set(by_id))

        # The per-record author lists go into the version ledger before
        # merging repoints them all to the survivor.
        author_counts: Counter[str] = Counter()
        version_authors: dict[str, list[VersionAuthor]] = {}
        known_persons: dict[str, Person] = {}
        for person in self.prepared.read_models("persons", Person):
            known_persons[person.id] = person
            for merged_id in person.merged_ids:
                known_persons.setdefault(merged_id, person)
            for authorship in person.authored:
                author_counts[authorship.publication_id] += 1
                version_authors.setdefault(authorship.publication_id, []).append(
                    VersionAuthor(person_id=person.id, name=person.name_en,
                                  position=authorship.position))
        for authors in version_authors.values():
            authors.sort(key=lambda author: (author.position is None,
                                             author.position, author.person_id))
        refreshed = self._refresh_version_ledger(publications, version_authors, known_persons)

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
                key=lambda publication: _publication_rank_key(
                    publication.type,
                    (publication.publication_date or date.min).toordinal(),
                    author_counts[publication.id],
                    publication.id,
                ),
            )
            canonical, duplicates = ranked[0], ranked[1:]
            canonical.versions = _merge_versions(
                canonical.versions,
                [_version_of(canonical, version_authors.get(canonical.id, ()))])
            for duplicate in duplicates:
                logger.info("dedup: merging publication %s (%s) into %s (%s)",
                            duplicate.id, duplicate.journal, canonical.id, canonical.journal)
                _merge_publication(canonical, duplicate,
                                   version_authors.get(duplicate.id, ()))
                canonical.merged_ids = _union(canonical.merged_ids, [duplicate.id])
                id_map[duplicate.id] = canonical.id
            self._record_state(canonical, len(duplicates))

        if not id_map:
            # Ledger rebuilds still have to reach disk when nothing merged.
            if refreshed:
                self.prepared.write_models("publications", publications)
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
        groups, report = plan_person_merges(
            people, self._trusted_orcids(people), self._person_scope(people),
            fields_of={
                publication.id: set(publication.fields)
                for publication in self.prepared.read_models("publications", Publication)
                if publication.fields
            })

        removed: set[str] = set()
        for canonical, duplicates in groups:
            for duplicate in duplicates:
                logger.info("dedup: merging %s (%s) into %s (%s)",
                            duplicate.id, duplicate.name_en, canonical.id, canonical.name_en)
                _merge_person(canonical, duplicate)
                if duplicate.name_en:
                    canonical.name_variants = _union(canonical.name_variants, [duplicate.name_en])
                canonical.merged_ids = _union(canonical.merged_ids, [duplicate.id])
                removed.add(duplicate.id)
            self._record_state(canonical, len(duplicates))
        if removed:
            people = [person for person in people if person.id not in removed]
            self.prepared.write_models("persons", people)

        held = sum(1 for row in report if row["status"] == "held")
        report_path = self.prepared.group_dir / CANDIDATES_FILENAME
        with AtomicWriter(report_path) as fh:
            for row in report:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        if report:
            logger.info("dedup: review journal in %s — %d merge(s) applied, %d pair(s) held",
                        report_path, len(removed), held)
        return len(removed), held

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
