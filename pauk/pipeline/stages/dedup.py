"""Merge prepared person rows that describe the same author.

OpenAlex author disambiguation sometimes splits one researcher into several
author records (e.g. "Nikolay Nikitin" and "Nikolay O. Nikitin"). Person
identity in PAUK is the OpenAlex author ID, so each split record becomes its
own person. This stage folds such duplicates back together using two rules:

1. ORCID: two persons carrying the same ORCID are the same author.
2. Name variant: one person's display name is listed among the other's
   OpenAlex name variants, both are ITMO-affiliated, and they share at
   least one coauthor.

Pairs with weaker evidence (an exact display-name match without variant
confirmation, or a variant match without a shared coauthor) are never merged
automatically — they are written to dedup_candidates.jsonl in the group
directory for manual review.

The stage is deterministic and purely local (no network), so unlike other
stages it re-examines the whole group on every run instead of tracking
per-row processing states; merges already applied simply produce no new
pairs. The merged OpenAlex IDs are recorded on the surviving person
(merged_ids) so that re-normalization keeps routing the old ID to the
canonical person and the graph loader can fold previously published
duplicate nodes.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from pauk.models import Person
from pauk.models.processing import ProcessingState, ProcessingStatus
from pauk.pipeline.normalize import _merge_person
from pauk.storage.atomic import AtomicWriter

from .base import EnrichmentStage

logger = logging.getLogger(__name__)

CANDIDATES_FILENAME = "dedup_candidates.jsonl"


def _norm(name: str | None) -> str:
    return " ".join((name or "").split()).casefold()


def _variant_set(person: Person) -> set[str]:
    return {_norm(variant) for variant in person.name_variants if _norm(variant)}


class DedupStage(EnrichmentStage):
    name = "dedup"

    def run(self) -> dict[str, int]:
        people = list(self.prepared.read_models("persons", Person))
        by_id = {person.id: person for person in people}
        in_scope = self._scope_ids(people)
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

        removed = self._apply_merges(by_id, merge_pairs, trusted_orcid)
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

        return {"dedup_merged": len(removed), "dedup_candidates": len(candidates)}

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

    def _scope_ids(self, people: list[Person]) -> set[str] | None:
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

    def _apply_merges(self, by_id: dict[str, Person], pairs: list[tuple[str, str]],
                      trusted_orcid: dict[str, str | None]) -> set[str]:
        """Union-find over merge pairs, then fold each group into a canonical.

        The canonical person is the one with the most authored works
        (ties: having an ORCID, then the smallest id, for determinism).

        Pairwise checks can't see transitive contradictions: A and B may
        each legitimately pair with a bridge person M yet carry different
        ORCIDs themselves. Any group holding more than one distinct ORCID
        is therefore skipped entirely and left for manual review.
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
        for person_id in parent:
            groups.setdefault(find(person_id), []).append(person_id)

        removed: set[str] = set()
        for members in groups.values():
            if len(members) < 2:
                continue
            distinct_orcids = {trusted_orcid.get(m) for m in members} - {None}
            if len(distinct_orcids) > 1:
                logger.warning(
                    "dedup: refusing to merge group %s — it spans %d distinct ORCIDs",
                    sorted(members), len(distinct_orcids))
                continue
            ranked = sorted(
                (by_id[m] for m in members),
                key=lambda p: (-len(p.authored), p.orcid is None, p.id),
            )
            canonical, duplicates = ranked[0], ranked[1:]
            for duplicate in duplicates:
                logger.info("dedup: merging %s (%s) into %s (%s)",
                            duplicate.id, duplicate.name_en, canonical.id, canonical.name_en)
                _merge_person(canonical, duplicate)
                if duplicate.name_en:
                    canonical.name_variants = list(dict.fromkeys(
                        [*canonical.name_variants, duplicate.name_en]))
                canonical.merged_ids = list(dict.fromkeys(
                    [*canonical.merged_ids, duplicate.id]))
                removed.add(duplicate.id)
            state = canonical.processing.get(self.name)
            canonical.processing[self.name] = ProcessingState(
                status=ProcessingStatus.COMPLETED,
                attempts=(state.attempts if state else 0) + 1,
                finished_at=datetime.now(timezone.utc),
                result_count=len(duplicates),
            )
        return removed
