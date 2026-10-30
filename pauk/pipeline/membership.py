"""Evidence of institutional affiliation, not a current-employment classifier."""

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pauk.models.person import Affiliation, MembershipEvidence, MembershipOrcidSource, Person
from pauk.models.processing import ProcessingStatus
from pauk.models.relations import Authorship

ITMO_ROR_ID = "04txgxn49"
POLICY_VERSION = "itmo-membership-v1"
_ROR = re.compile(r"(?:https?://ror\.org/)?(0[a-z0-9]{8})/?", re.IGNORECASE)
logger = logging.getLogger(__name__)


def normalize_ror(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    match = _ROR.fullmatch(value.strip())
    return match.group(1).lower() if match else None


def authorship_institution_rors(authorship: dict) -> list[str] | None:
    institutions = authorship.get("institutions")
    if not isinstance(institutions, list):
        return None
    rors = [normalize_ror(row.get("ror")) if isinstance(row, dict) else None for row in institutions]
    # Partial institution resolution cannot establish a negative result.
    if None in rors and ITMO_ROR_ID not in rors:
        return None
    return sorted({ror for ror in rors if ror is not None})


@dataclass(frozen=True)
class MembershipResult:
    status: Literal["supported", "unsupported", "unknown"]
    is_itmo: bool
    evidence: list[MembershipEvidence]
    policy: str = POLICY_VERSION


def refresh_authorship_membership(
    person: Person, row: Authorship, raw_authorship: dict,
    source_person_id: str, source_work_id: str,
    observed_at: datetime | None = None,
    *, snapshot_accepted: bool = False,
) -> None:
    """Replace only a source slot that actually supplied institution evidence."""
    rors = authorship_institution_rors(raw_authorship)
    if rors is None:
        return
    if not snapshot_accepted and not authorship_snapshot_is_current(person, source_person_id, source_work_id, observed_at):
        if source_observed_at(observed_at) == source_observed_at(row.institution_observed_at) and rors != row.institution_rors:
            logger.warning("Conflicting equal-time authorship snapshot for %s / %s", source_person_id, source_work_id)
        return
    person.membership_evidence = [
        claim for claim in person.membership_evidence
        if not (
            claim.source == "openalex_work"
            and claim.source_person_id == source_person_id
            and claim.source_id == source_work_id
            and claim.position == row.position
        )
    ]
    row.institution_rors = rors
    row.institution_source_person_id = source_person_id
    row.institution_source_id = source_work_id
    row.institution_observed_at = observed_at


def source_observed_at(value: object) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def authorship_snapshot_is_current(
    person: Person, source_person_id: str, source_work_id: str, observed_at: datetime | None,
) -> bool:
    incoming = source_observed_at(observed_at)
    previous = [
        stamp for row in person.authored
        if row.institution_source_person_id == source_person_id and row.institution_source_id == source_work_id
        and (stamp := source_observed_at(row.institution_observed_at)) is not None
    ]
    return not previous or incoming is not None and incoming > max(previous)


def reconcile_authorship_snapshot(
    person: Person, source_work_id: str,
    slots: dict[tuple[str, int], list[str] | None], observed_at: datetime | None,
    *, complete: bool,
) -> None:
    """Retire superseded membership claims without deleting authorship history."""
    owners = {owner for owner, _ in slots}
    current_owners = {
        row.institution_source_person_id for row in person.authored
        if row.institution_source_id == source_work_id and row.institution_source_person_id is not None
        and authorship_snapshot_is_current(person, row.institution_source_person_id, source_work_id, observed_at)
    }
    for row in person.authored:
        owner = row.institution_source_person_id
        if row.institution_source_id != source_work_id or owner is None:
            continue
        if owner not in current_owners:
            expected = slots.get((owner, row.position))
            if source_observed_at(observed_at) == source_observed_at(row.institution_observed_at) and (
                expected is not None and expected != row.institution_rors
                or complete and owner not in owners and row.institution_rors
            ):
                logger.warning("Conflicting equal-time authorship snapshot for %s / %s", owner, source_work_id)
            continue
        key = (owner, row.position)
        if key in slots:
            continue
        if not complete:
            continue
        if owner in owners and any(rors is None for (source_owner, _), rors in slots.items() if source_owner == owner):
            continue
        refresh_authorship_membership(
            person, row, {"institutions": []}, owner, source_work_id, observed_at, snapshot_accepted=True,
        )


def orcid_source_owner(person: Person, source_id: str) -> str | None:
    """A merged scalar ORCID does not identify which original person supplied it."""
    if not person.merged_ids:
        return person.id
    owners = {
        row.source_person_id
        for row in [*person.affiliations, *person.membership_evidence]
        if row.source == "orcid" and row.source_id == source_id
        and row.source_person_id in {person.id, *person.merged_ids}
    }
    owners.update(
        row.source_person_id for row in person.membership_orcid_sources
        if row.source_id == source_id and row.source_person_id in {person.id, *person.merged_ids}
    )
    return next(iter(owners)) if len(owners) == 1 else None


def refresh_orcid_membership(
    person: Person, affiliations: list[Affiliation], source_person_id: str | None, source_id: str,
    *, observed_at: datetime | None = None,
) -> None:
    """A successful ORCID response supersedes this owner's earlier response."""
    def belongs_to_source(owner: str | None, record: str | None) -> bool:
        if source_person_id is None:
            return owner is None and record == source_id
        return owner == source_person_id and record in {None, source_id} or (
            owner is None and not person.merged_ids and record in {None, source_id}
        )

    person.affiliations = [
        row for row in person.affiliations
        if not (row.source == "orcid" and belongs_to_source(row.source_person_id, row.source_id))
    ]
    person.membership_evidence = [
        row for row in person.membership_evidence
        if not (row.source == "orcid" and belongs_to_source(row.source_person_id, row.source_id))
    ]
    person.membership_orcid_sources = [
        row for row in person.membership_orcid_sources
        if (row.source_person_id, row.source_id) != (source_person_id, source_id)
    ]
    person.membership_orcid_sources.append(MembershipOrcidSource(
        source_id=source_id, source_person_id=source_person_id, observed_at=source_observed_at(observed_at),
    ))
    for affiliation in affiliations:
        row = affiliation.model_copy(deep=True)
        row.source_person_id = source_person_id
        row.source_id = source_id
        person.affiliations.append(row)


def evaluate_membership(person: Person) -> MembershipResult:
    """Retain historical evidence when a group contains only a partial view.

    Successful source refreshes retract their own claims before evaluation.
    Legacy booleans and incomplete source coverage never prove a negative.
    """
    refreshed_work_keys = {
        (row.institution_source_person_id, row.institution_source_id or row.publication_id, row.position)
        for row in person.authored if row.institution_rors is not None
    }
    evidence = [
        row.model_copy(deep=True) for row in person.membership_evidence
        if normalize_ror(row.ror) == ITMO_ROR_ID
        and not (row.source == "openalex_work"
                 and (row.source_person_id, row.source_id, row.position) in refreshed_work_keys)
    ]
    for row in person.authored:
        if any(normalize_ror(ror) == ITMO_ROR_ID for ror in row.institution_rors or []):
            claim = MembershipEvidence(
                source="openalex_work", source_person_id=row.institution_source_person_id,
                ror=ITMO_ROR_ID, publication_id=row.publication_id,
                source_id=row.institution_source_id or row.publication_id, position=row.position,
            )
            if claim not in evidence:
                evidence.append(claim)
    for row in person.affiliations:
        if row.source == "orcid" and normalize_ror(row.ror) == ITMO_ROR_ID:
            claim = MembershipEvidence(
                source="orcid", source_person_id=row.source_person_id,
                ror=ITMO_ROR_ID, source_id=row.source_id,
            )
            if claim not in evidence:
                evidence.append(claim)
    if evidence:
        return MembershipResult("supported", True, evidence)
    orcid_state = person.processing.get("orcid")
    orcid_complete = not person.orcid or (
        orcid_state is not None
        and orcid_state.status in {ProcessingStatus.COMPLETED, ProcessingStatus.COMPLETED_EMPTY}
        and orcid_state.request_key == person.orcid
    )
    work_complete = bool(person.authored) and all(
        row.institution_rors is not None for row in person.authored
    )
    legacy_positive = person.is_itmo and person.membership_status in {None, "unknown"}
    if work_complete and orcid_complete and not person.merged_ids and not legacy_positive:
        return MembershipResult("unsupported", False, [])
    return MembershipResult("unknown", person.is_itmo, [])


def apply_membership(person: Person) -> None:
    result = evaluate_membership(person)
    person.membership_status = result.status
    person.membership_policy = result.policy
    person.membership_evidence = result.evidence
    person.is_itmo = result.is_itmo
