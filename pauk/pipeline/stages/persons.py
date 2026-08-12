from datetime import UTC, datetime

from pauk.models import Affiliation, Person, Publication
from pauk.models.processing import ProcessingState, ProcessingStatus
from pauk.pipeline.normalize import ITMO_ROR_ID
from pauk.redaction import redact_text
from pauk.sources import OpenAlexClient
from pauk.sources.crossref import CrossrefClient
from pauk.sources.openreview import OpenReviewClient
from pauk.sources.orcid import OrcidClient

from .base import EnrichmentStage


def _openalex_affiliations(payload: dict) -> list[Affiliation]:
    """Where the author's own OpenAlex record says they worked."""
    affiliations = []
    for entry in payload.get("affiliations") or []:
        institution = entry.get("institution") or {}
        if institution.get("display_name"):
            affiliations.append(Affiliation(
                name=institution["display_name"],
                ror=(institution.get("ror") or "").rstrip("/").rsplit("/", 1)[-1] or None,
                years=[int(year) for year in entry.get("years") or []],
                source="openalex",
            ))
    # last_known_institutions carries no years: it is the fallback for works
    # from a year no affiliation entry covers.
    for institution in payload.get("last_known_institutions") or []:
        if institution.get("display_name"):
            affiliations.append(Affiliation(
                name=institution["display_name"],
                ror=(institution.get("ror") or "").rstrip("/").rsplit("/", 1)[-1] or None,
                source="openalex",
            ))
    return affiliations


def _orcid_affiliations(record: dict) -> list[Affiliation]:
    """Employments the author listed on their own ORCID profile."""
    employments = ((record.get("activities-summary") or {}).get("employments") or {})
    affiliations = []
    for group in employments.get("affiliation-group") or []:
        for summary in group.get("summaries") or []:
            employment = summary.get("employment-summary") or {}
            organization = employment.get("organization") or {}
            name = organization.get("name")
            if not name:
                continue
            disambiguated = organization.get("disambiguated-organization") or {}
            ror = None
            if (disambiguated.get("disambiguation-source") or "").upper() == "ROR":
                ror = (disambiguated.get("disambiguated-organization-identifier") or "").rstrip("/").rsplit("/", 1)[-1]
            start = ((employment.get("start-date") or {}).get("year") or {}).get("value")
            end = ((employment.get("end-date") or {}).get("year") or {}).get("value")
            years = []
            if start:
                # An open-ended employment covers everything from its start;
                # the year picker only needs the range to contain the work.
                years = list(range(int(start), int(end or datetime.now(UTC).year) + 1))
            affiliations.append(Affiliation(name=name, ror=ror or None, years=years, source="orcid"))
    return affiliations


def _merge_affiliations(*sources: list[Affiliation]) -> list[Affiliation]:
    merged: dict[tuple[str, str], Affiliation] = {}
    for affiliations in sources:
        for affiliation in affiliations:
            existing = merged.get((affiliation.name, affiliation.source))
            if existing is None:
                merged[(affiliation.name, affiliation.source)] = affiliation
                continue
            existing.years = sorted({*existing.years, *affiliation.years})
            existing.ror = existing.ror or affiliation.ror
    return list(merged.values())


def _affiliation_for_year(affiliations: list[Affiliation], year: int | None) -> Affiliation | None:
    """The affiliation to place an authorship by.

    An affiliation whose years cover the work wins; failing that the most
    recently dated one, and failing that one without years at all (that is
    what last_known_institutions is).
    """
    if not affiliations:
        return None
    if year is not None:
        covering = [a for a in affiliations if year in a.years]
        if covering:
            return covering[0]
    dated = [a for a in affiliations if a.years]
    if dated:
        return max(dated, key=lambda a: max(a.years))
    return affiliations[0]


class PersonsStage(EnrichmentStage):
    name = "persons"
    crossref_name = "crossref"

    def _people_in_scope(self, people: list[Person]) -> list[Person]:
        if self.selection is None:
            return people
        if self.selection.entity == "persons":
            return [person for person in people if person.id in self.selection.ids]
        if self.selection.entity == "publications":
            return [
                person for person in people
                if any(authorship.publication_id in self.selection.ids for authorship in person.authored)
            ]
        return []

    def _fill_missing_affiliations(self, person: Person, publications: dict[str, Publication]) -> int:
        """Place authorships the work itself left without an affiliation.

        Self-deposited records (Zenodo, SSRN) often name a coauthor without
        saying where they work. The author's own records do know, so the
        authorship borrows the affiliation of its own year and records that
        it was filled in rather than stated by the work.
        """
        filled = 0
        for authorship in person.authored:
            if authorship.affiliation:
                continue
            publication = publications.get(authorship.publication_id)
            affiliation = _affiliation_for_year(
                person.affiliations, publication.year if publication else None)
            if affiliation is None:
                continue
            authorship.affiliation = affiliation.name
            authorship.affiliation_source = affiliation.source
            filled += 1
        return filled

    def run(self) -> dict[str, int]:
        people = list(self.prepared.read_models("persons", Person))
        publications = {
            publication.id: publication
            for publication in self.prepared.read_models("publications", Publication)
        }
        eligible_people = self._people_in_scope(people)
        candidates = [
            person for person in eligible_people
            if self.needs_attempt(person.processing.get(self.name))
        ]
        by_publication: dict[str, list[Person]] = {}
        for person in eligible_people:
            for authored in person.authored:
                publication = publications.get(authored.publication_id)
                if publication and self.needs_attempt(publication.processing.get(self.crossref_name)):
                    by_publication.setdefault(authored.publication_id, []).append(person)

        crossref = CrossrefClient(self.config.request_timeout)
        crossref_changed = 0
        for publication_id, authors in by_publication.items():
            publication = publications.get(publication_id)
            if publication is None or not self.needs_attempt(publication.processing.get(self.crossref_name)):
                continue
            old_state = publication.processing.get(self.crossref_name)
            if not publication.doi:
                publication.processing[self.crossref_name] = ProcessingState(
                    status=ProcessingStatus.NOT_APPLICABLE,
                    attempts=(old_state.attempts if old_state else 0) + 1,
                    finished_at=datetime.now(UTC), result_count=0,
                )
                crossref_changed += 1
                continue
            try:
                payload = crossref.get_work(publication.doi)
                self.raw.append("crossref", payload, {"doi": publication.doi})
                matches_count = 0
                for author in payload.get("message", {}).get("author", []):
                    family = (author.get("family") or "").casefold()
                    orcid = (author.get("ORCID") or "").rstrip("/").split("/")[-1] or None
                    matches = [
                        person for person in authors
                        if person.name_en and person.name_en.split()[-1].casefold() == family
                    ]
                    if orcid and len(matches) == 1 and not matches[0].orcid:
                        matches[0].orcid = orcid
                        matches_count += 1
                publication.processing[self.crossref_name] = ProcessingState(
                    status=ProcessingStatus.COMPLETED if matches_count else ProcessingStatus.COMPLETED_EMPTY,
                    attempts=(old_state.attempts if old_state else 0) + 1,
                    finished_at=datetime.now(UTC), result_count=matches_count,
                )
            except Exception as exc:
                publication.processing[self.crossref_name] = ProcessingState(
                    status=ProcessingStatus.FAILED,
                    attempts=(old_state.attempts if old_state else 0) + 1,
                    finished_at=datetime.now(UTC), error=redact_text(exc),
                )
            crossref_changed += 1

        client = OpenAlexClient(self.config.request_timeout, self.config.openalex_api_key)
        orcid_client = OrcidClient(self.config.request_timeout)
        openreview = OpenReviewClient(
            self.config.request_timeout,
            self.config.openreview_username,
            self.config.openreview_password,
        )
        changed = 0
        for person in candidates:
            state = person.processing.get(self.name)
            try:
                before = person.model_dump(exclude={"processing", "authored", "contributed_to"})
                # Authors OpenAlex has not disambiguated yet have no author
                # record to fetch (normalize keys them by ORCID or name); the
                # enrichment below still applies to whatever they do carry.
                if person.openalex_id:
                    payload = client.get_author(person.openalex_id)
                    self.raw.append("openalex_authors", payload, {"author_id": person.openalex_id})
                    person.name_en = payload.get("display_name") or person.name_en
                    person.name_variants = list(dict.fromkeys([
                        *person.name_variants, *(payload.get("display_name_alternatives") or []),
                    ]))
                    person.orcid = person.orcid or ((payload.get("orcid") or "").rstrip("/").split("/")[-1] or None)
                    person.affiliations = _merge_affiliations(
                        person.affiliations, _openalex_affiliations(payload))
                if person.orcid:
                    record = orcid_client.get_record(person.orcid)
                    self.raw.append("orcid", record, {"orcid": person.orcid})
                    emails = (((record.get("person") or {}).get("emails") or {}).get("email") or [])
                    if not person.email:
                        person.email = next((item.get("email") for item in emails if item.get("email")), None)
                    person.affiliations = _merge_affiliations(
                        person.affiliations, _orcid_affiliations(record))
                # An ITMO affiliation the works never stated still makes the
                # person ITMO — the ROR is the same identifier normalize uses.
                person.is_itmo = person.is_itmo or any(
                    affiliation.ror == ITMO_ROR_ID for affiliation in person.affiliations)
                self._fill_missing_affiliations(person, publications)
                if person.is_itmo and person.name_en and self.config.openreview_username and self.config.openreview_password:
                    openreview_payload = openreview.search(person.name_en)
                    self.raw.append("openreview", openreview_payload, {"term": person.name_en})
                    for profile in openreview_payload.get("profiles", []):
                        content = profile.get("content") or {}
                        emails = content.get("emails") or []
                        profile_orcid = (content.get("orcid") or "").rstrip("/").split("/")[-1]
                        if profile_orcid == person.orcid or any(str(email).endswith("@itmo.ru") for email in emails):
                            person.openreview = profile.get("id")
                            person.github = person.github or (content.get("github") or "").rstrip("/").split("/")[-1] or None
                            person.google_scholar = person.google_scholar or content.get("gscholar")
                            break
                after = person.model_dump(exclude={"processing", "authored", "contributed_to"})
                result_count = sum(before[key] != after[key] for key in after)
                person.processing[self.name] = ProcessingState(
                    status=ProcessingStatus.COMPLETED if result_count else ProcessingStatus.COMPLETED_EMPTY,
                    attempts=(state.attempts if state else 0) + 1,
                    finished_at=datetime.now(UTC), result_count=result_count,
                )
            except Exception as exc:
                person.processing[self.name] = ProcessingState(
                    status=ProcessingStatus.FAILED,
                    attempts=(state.attempts if state else 0) + 1,
                    finished_at=datetime.now(UTC), error=redact_text(exc),
                )
            changed += 1
        self.prepared.write_models("persons", people)
        self.prepared.write_models("publications", publications.values())
        return {"persons": changed, "crossref": crossref_changed}
