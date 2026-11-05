"""Read-only reconstruction of membership inventories from global stored sources."""

import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from pymongo.database import Database

from pauk.models.person import MembershipOwnerSources, MembershipWorkSource, Person
from pauk.pipeline.membership import (
    POLICY_VERSION,
    apply_membership,
    authorship_institution_rors,
    iter_work_membership,
    membership_sources_complete,
    refresh_orcid_membership,
    source_observed_at,
)
from pauk.pipeline.normalize import _authorship_person_id, _short_id
from pauk.pipeline.stages.persons import _orcid_affiliations


def _latest(rows: list[dict]) -> dict | None:
    """Equal-time contradictory responses cannot establish a negative result."""
    if not rows or any(source_observed_at(row.get("fetched_at")) is None for row in rows):
        return None
    newest = max(source_observed_at(row["fetched_at"]) for row in rows)
    latest = [row for row in rows if source_observed_at(row["fetched_at"]) == newest]
    values = {json.dumps(row.get("payload"), sort_keys=True, default=str) for row in latest}
    return latest[0] if len(values) == 1 else None


@dataclass(frozen=True)
class MembershipSourceIndex:
    works: dict[str, dict | None]
    orcids: dict[str, dict | None]
    profile_orcids: dict[str, set[str]]
    profile_owners: set[str]
    work_owners: dict[str, set[str]]
    work_orcids: dict[str, set[str]]
    aliases: dict[str, str]
    publication_sources: dict[str, set[str]]


def build_membership_source_index(
    raw_rows: Iterable[dict], publication_rows: Iterable[dict],
) -> MembershipSourceIndex:
    """Index a global snapshot once for reassessing any number of people."""
    works: dict[str, list[dict]] = defaultdict(list)
    orcids: dict[str, list[dict]] = defaultdict(list)
    profile_orcids: dict[str, set[str]] = defaultdict(set)
    profile_owners = set()
    work_owners: dict[str, set[str]] = defaultdict(set)
    work_orcids: dict[str, set[str]] = defaultdict(set)
    for envelope in raw_rows:
        payload = envelope.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        source = envelope.get("source")
        if source == "openalex_works" and (work := _short_id(payload.get("id"))):
            works[work].append(envelope)
            for row in payload.get("authorships") or []:
                if not isinstance(row, dict):
                    continue
                author = row.get("author") or {}
                owner = _authorship_person_id(author)
                if owner:
                    work_owners[owner].add(work)
                    if orcid := _short_id(author.get("orcid")):
                        work_orcids[owner].add(orcid)
        elif source == "openalex_authors":
            owner = _short_id(payload.get("id"))
            if owner:
                profile_owners.add(owner)
                if orcid := _short_id(payload.get("orcid")):
                    profile_orcids[owner].add(orcid)
        elif source == "orcid":
            source_id = _short_id((envelope.get("request") or {}).get("orcid"))
            if source_id:
                orcids[source_id].append(envelope)
    aliases = {}
    publication_sources: dict[str, set[str]] = defaultdict(set)
    for publication in publication_rows:
        for source_id in [publication.get("id"), *(publication.get("merged_ids") or [])]:
            if source_id:
                aliases[source_id] = publication["id"]
                publication_sources[publication["id"]].add(source_id)
    return MembershipSourceIndex(
        works={key: _latest(rows) for key, rows in works.items()},
        orcids={key: _latest(rows) for key, rows in orcids.items()},
        profile_orcids=dict(profile_orcids), profile_owners=profile_owners,
        work_owners=dict(work_owners), work_orcids=dict(work_orcids),
        aliases=aliases, publication_sources=dict(publication_sources),
    )


def rebuild_from_global_sources(
    db: Database, person: Person, *, raw_rows: Iterable[dict] | None = None,
    source_index: MembershipSourceIndex | None = None,
) -> Person:
    """Return a reassessed copy; never mutate Mongo or infer ownership from a merged name.

    Negative reconstruction requires independent rows for every original owner,
    or their already established inventories, and all referenced raw responses.
    Missing or ambiguous history leaves the previous classification unresolved.
    """
    result = person.model_copy(deep=True)
    owners = {person.id, *person.merged_ids}
    originals: dict[str, list[dict]] = defaultdict(list)
    for row in db.persons.find({"id": {"$in": sorted(owners)}}):
        if not row.get("merged_ids"):
            originals[row["id"]].append(row)
    if not person.merged_ids:
        originals[person.id].append(person.model_dump())
    ambiguous_owners = set()
    archive_versions: dict[tuple, str] = {}
    for entry in db.person_merge_sources.find({"person_id": {"$in": sorted(owners)}}):
        row = entry.get("snapshot") or {}
        owner = entry.get("person_id")
        if row.get("id") != owner or row.get("merged_ids"):
            continue
        key = (owner, entry.get("saved_at"), row.get("_version"))
        content = json.dumps(row, sort_keys=True, default=str)
        if key in archive_versions and archive_versions[key] != content:
            ambiguous_owners.add(owner)
        archive_versions[key] = content
        originals[owner].append(row)
    if source_index is None:
        envelopes = raw_rows if raw_rows is not None else db.raw.find({
            "source": {"$in": ["openalex_works", "openalex_authors", "orcid"]},
        })
        source_index = build_membership_source_index(
            envelopes, db.publications.find({}, {"id": 1, "merged_ids": 1}),
        )
    works, orcids = source_index.works, source_index.orcids
    aliases = source_index.aliases
    inventories = {row.source_person_id: row for row in person.membership_owner_sources}
    manifests = []
    for owner in sorted(owners):
        original = originals[owner]
        inventory = inventories.get(owner)
        inventoried = (inventory is not None and inventory.policy == POLICY_VERSION
                       and inventory.work_ids is not None and inventory.orcid_ids is not None)
        known = bool(original) or inventoried
        expected_works = set(source_index.work_owners.get(owner, set())) | set(inventory.work_ids or [] if inventory else [])
        expected_orcids = set(source_index.profile_orcids.get(owner, set())) | set(source_index.work_orcids.get(owner, set()))
        if inventory:
            expected_orcids.update(inventory.orcid_ids or [])
        current_sources = {
            "membership_work_sources": [row.model_dump() for row in iter_work_membership(person)
                                        if row.institution_source_person_id == owner],
            "membership_evidence": [row.model_dump() for row in person.membership_evidence
                                    if row.source_person_id == owner],
            "membership_orcid_sources": [row.model_dump() for row in person.membership_orcid_sources
                                         if row.source_person_id == owner],
            "affiliations": [row.model_dump() for row in person.affiliations if row.source_person_id == owner],
        }
        for original_row in [*original, current_sources]:
            if source_id := _short_id(original_row.get("orcid")):
                expected_orcids.add(source_id)
            for authored in [*(original_row.get("authored") or []),
                             *(original_row.get("membership_work_sources") or [])]:
                work = authored.get("institution_source_id") or authored.get("publication_id")
                if work:
                    expected_works.add(work)
                    expected_works.update(source_index.publication_sources.get(work, set()))
            for claim in [*(original_row.get("affiliations") or []),
                          *(original_row.get("membership_evidence") or []),
                          *({"source": "orcid", **row} for row in original_row.get("membership_orcid_sources") or [])]:
                source = claim.get("source")
                if source not in {"orcid", "openalex_work"}:
                    continue
                if claim.get("source_person_id") not in {None, owner}:
                    known = False
                    continue
                source_id = claim.get("source_id") or (
                    original_row.get("orcid") if source == "orcid" else claim.get("publication_id")
                )
                if not source_id:
                    known = False
                elif source == "orcid":
                    expected_orcids.add(_short_id(source_id))
                else:
                    expected_works.add(source_id)
        # An empty legacy card is not proof that no source ever established its flag.
        known = known and (inventoried or owner in source_index.profile_owners or bool(expected_works & works.keys())
                           or bool(expected_orcids & orcids.keys()))
        known = known and owner not in ambiguous_owners
        manifests.append(MembershipOwnerSources(
            source_person_id=owner, work_ids=sorted(expected_works) if known else None,
            orcid_ids=sorted(expected_orcids) if known else None, observed_at=datetime.now(UTC),
        ))
    result.membership_owner_sources = manifests
    candidate = result.model_copy(deep=True)
    candidate.membership_work_sources = []
    candidate.membership_orcid_sources = []
    candidate.membership_evidence = []
    candidate.affiliations = [row for row in candidate.affiliations if row.source != "orcid"]
    complete = all(row.work_ids is not None and row.orcid_ids is not None for row in manifests)
    for claim in [*person.membership_evidence, *(row for row in person.affiliations if row.source == "orcid")]:
        matching = [inventory for inventory in manifests
                    if claim.source_person_id in {None, inventory.source_person_id}
                    and claim.source_id in ((inventory.orcid_ids if claim.source == "orcid" else inventory.work_ids) or [])]
        if not matching:
            # Untagged facts are removable only if an independent original retained the same fact.
            original_match = any(
                (claim.source == "orcid" and item.get("source") == "orcid"
                 and item.get("name") == getattr(claim, "name", None) and item.get("ror") == claim.ror)
                for rows in originals.values() for original in rows for item in original.get("affiliations") or []
            )
            if not original_match:
                complete = False
    for inventory in manifests:
        owner = inventory.source_person_id
        for work in inventory.work_ids or []:
            envelope = works.get(work)
            if envelope is None:
                complete = False
                continue
            payload = envelope["payload"]
            authored = payload.get("authorships")
            count = payload.get("authors_count")
            if (not isinstance(authored, list) or payload.get("is_authors_truncated")
                    or count is not None and (not isinstance(count, int) or count > len(authored))):
                complete = False
                continue
            if any(not isinstance(row, dict) or not _authorship_person_id(row.get("author") or {}) for row in authored):
                complete = False
                continue
            observed_at = source_observed_at(envelope["fetched_at"])
            rows = [(position, row) for position, row in enumerate(authored, 1)
                    if _authorship_person_id(row.get("author") or {}) == owner]
            # A complete work response may remove an earlier author; retain a tombstone.
            if not rows:
                positions = {row.position for row in iter_work_membership(person)
                             if row.institution_source_person_id == owner and row.institution_source_id == work}
                rows = [(position, {"institutions": []}) for position in positions or {None}]
            for position, row in rows:
                candidate.membership_work_sources.append(MembershipWorkSource(
                    publication_id=aliases.get(work, work), position=position,
                    institution_rors=authorship_institution_rors(row),
                    institution_source_person_id=owner, institution_source_id=work,
                    institution_observed_at=observed_at,
                ))
            previous = [row for row in iter_work_membership(person)
                        if row.institution_source_person_id == owner and row.institution_source_id == work]
            if any((stamp := source_observed_at(row.institution_observed_at)) is not None and stamp > observed_at
                   for row in previous):
                complete = False
            new_values = {(row.position, tuple(row.institution_rors or []))
                          for row in candidate.membership_work_sources
                          if row.institution_source_person_id == owner and row.institution_source_id == work}
            if any(source_observed_at(row.institution_observed_at) == observed_at
                   and (row.position, tuple(row.institution_rors or [])) not in new_values for row in previous):
                complete = False
        for source_id in inventory.orcid_ids or []:
            envelope = orcids.get(source_id)
            employments = (((envelope or {}).get("payload") or {}).get("activities-summary") or {}).get("employments")
            if not isinstance(employments, dict) or not isinstance(employments.get("affiliation-group"), list):
                complete = False
                continue
            try:
                affiliations = _orcid_affiliations(envelope["payload"])
            except (ValueError, TypeError, AttributeError):
                complete = False
                continue
            observed_at = source_observed_at(envelope["fetched_at"])
            previous = [row for row in person.membership_orcid_sources
                        if (row.source_person_id, row.source_id) == (owner, source_id)]
            if any((stamp := source_observed_at(row.observed_at)) is not None and stamp > observed_at for row in previous):
                complete = False
            if any(source_observed_at(row.observed_at) == observed_at for row in previous):
                old_affiliations = sorted((row.name, row.ror or "", tuple(row.years)) for row in person.affiliations
                                          if row.source == "orcid" and (row.source_person_id, row.source_id) == (owner, source_id))
                new_affiliations = sorted((row.name, row.ror or "", tuple(row.years)) for row in affiliations)
                if old_affiliations != new_affiliations:
                    complete = False
            refresh_orcid_membership(candidate, affiliations, owner, source_id,
                                     observed_at=observed_at)
    if complete and membership_sources_complete(candidate):
        result = candidate
    else:
        # A failed audit must not publish a completeness certificate assembled from partial history.
        for inventory in result.membership_owner_sources:
            inventory.work_ids = None
    apply_membership(result)
    return result
