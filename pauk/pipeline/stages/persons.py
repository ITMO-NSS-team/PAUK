import logging
import re
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

logger = logging.getLogger(__name__)


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


# Where a researcher-url points, and which field of the person it fills.
# A GitHub link here is the strongest tie there is: the author stated it
# themselves, so no matching is involved.
PROFILE_HOSTS = (
    ("github.com", "github"),
    ("scholar.google", "google_scholar"),
    ("gitlab.com", "gitlab_username"),
    ("linkedin.com", "linkedin"),
)

# Directories and indexes an author lists beside their own page. None of
# them is a homepage, and none carries an address worth collecting.
NOT_A_HOMEPAGE = (
    "researchgate.net", "webofscience.com", "scopus.com", "publons.com",
    "twitter.com", "x.com", "orcid.org", "ncbi.nlm.nih.gov", "semanticscholar",
    "dblp.org", "facebook", "youtube", "t.me", "vk.com",
)


def _github_login(url: str) -> str | None:
    """The account name inside a GitHub URL.

    Authors paste the address bar as it stands, tab and all —
    "github.com/coralr-1?tab=repositories" — and a link to a repository
    rather than a profile names no account at all.
    """
    match = re.match(r"https?://(?:www\.)?github\.com/([A-Za-z0-9-]+)/?$",
                     url.split("?")[0].split("#")[0].strip())
    return match.group(1) if match else None


def _orcid_profiles(record: dict) -> dict[str, str]:
    """Profiles the author linked from their own ORCID record.

    Only the first of each kind is taken: a second GitHub link is another
    account of theirs, and choosing between them is guesswork the person
    did not ask for.
    """
    person = record.get("person") or {}
    found: dict[str, str] = {}
    for entry in (person.get("researcher-urls") or {}).get("researcher-url", []) or []:
        url = ((entry.get("url") or {}).get("value") or "").strip()
        if not url:
            continue
        lowered = url.lower()
        for host, field in PROFILE_HOSTS:
            if host in lowered:
                found.setdefault(field, url)
                break
        else:
            if not any(host in lowered for host in NOT_A_HOMEPAGE):
                found.setdefault("homepage", url)
    return found


def _orcid_other_names(record: dict, known: list[str]) -> list[str]:
    """Every alternative spelling ORCID holds for the author."""
    person = record.get("person") or {}
    name = person.get("name") or {}
    found = {(name.get("credit-name") or {}).get("value")}
    found |= {entry.get("content")
              for entry in (person.get("other-names") or {}).get("other-name", [])}
    return sorted({value.strip() for value in {*known, *found} if value and value.strip()})


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
    progress_label = "Authors: enriching profiles from OpenAlex, ORCID, and OpenReview"
    crossref_name = "crossref"
    openalex_name = "openalex_author"
    orcid_name = "orcid"
    openreview_name = "openreview"
    openreview_batch_size = 1000

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

    def _needs_source(self, state: ProcessingState | None, request_key: str) -> bool:
        return self.force or state is None or state.request_key != request_key or self.needs_attempt(state)

    @staticmethod
    def _next_attempt(state: ProcessingState | None) -> int:
        return (state.attempts if state else 0) + 1

    def _save_person(self, person: Person) -> None:
        self.prepared.upsert_models("persons", [person])

    @staticmethod
    def _profile_matches(person: Person, profile: dict) -> bool:
        content = profile.get("content") or {}
        profile_orcid = (content.get("orcid") or "").rstrip("/").split("/")[-1]
        emails = [
            *(content.get("emails") or []), *(content.get("emailsConfirmed") or []),
            *(profile.get("confirmedEmails") or []), profile.get("email") or "",
        ]
        return profile_orcid == person.orcid or any(str(email).endswith("@itmo.ru") for email in emails)

    @staticmethod
    def _apply_profile(person: Person, profile: dict) -> None:
        content = profile.get("content") or {}
        person.openreview = profile.get("id")
        person.github = person.github or (content.get("github") or "").rstrip("/").split("/")[-1] or None
        person.google_scholar = person.google_scholar or content.get("gscholar")

    def _name_phase(self, person: Person, publications: dict[str, Publication]) -> str:
        fields = {
            field.casefold() for authorship in person.authored
            for field in (publications.get(authorship.publication_id).fields if publications.get(authorship.publication_id) else [])
        }
        return "name_cs_ml" if fields & self.config.openreview_priority_field_set else "name_remaining"

    def _run_name_searches(self, people: list[Person], phase: str, client: OpenReviewClient) -> tuple[int, int]:
        candidates = [
            person for person in people
            if (state := person.processing.get(self.openreview_name))
            and state.phase == phase and person.name_raw
            and self._needs_source(state, person.name_raw.casefold())
        ]
        changed = found = 0
        for person in self.progress(candidates, total=len(candidates), label=f"OpenReview: {phase}"):
            state = person.processing.get(self.openreview_name)
            key = person.name_raw.casefold()
            try:
                payload = client.search(person.name_raw)
                self.raw.append("openreview", payload, {"method": "name_search", "phase": phase, "term": person.name_raw})
                profile = next((p for p in payload.get("profiles", []) if self._profile_matches(person, p)), None)
                if profile:
                    self._apply_profile(person, profile)
                    found += 1
                person.processing[self.openreview_name] = ProcessingState(
                    status=ProcessingStatus.COMPLETED if profile else ProcessingStatus.COMPLETED_EMPTY,
                    request_key=key, phase=phase, attempts=self._next_attempt(state),
                    finished_at=datetime.now(UTC), result_count=int(bool(profile)),
                )
            except Exception as exc:
                person.processing[self.openreview_name] = ProcessingState(
                    status=ProcessingStatus.FAILED, request_key=key, phase=phase,
                    attempts=self._next_attempt(state), finished_at=datetime.now(UTC), error=redact_text(exc),
                )
            self._save_person(person)
            changed += 1
        return changed, found

    def run(self) -> dict[str, int]:
        people = list(self.prepared.read_models("persons", Person))
        publications = {row.id: row for row in self.prepared.read_models("publications", Publication)}
        eligible_people = self._people_in_scope(people)
        by_publication: dict[str, list[Person]] = {}
        for person in eligible_people:
            for authored in person.authored:
                if publication := publications.get(authored.publication_id):
                    by_publication.setdefault(publication.id, []).append(person)

        crossref_changed = 0
        crossref = CrossrefClient(self.config.request_timeout)
        for publication_id, authors in self.progress(by_publication.items(), total=len(by_publication),
                                                      label="Publications: finding author ORCIDs via Crossref",
                                                      unit="publication"):
            publication = publications[publication_id]
            state = publication.processing.get(self.crossref_name)
            key = publication.doi or ""
            if not self._needs_source(state, key):
                continue
            if not publication.doi:
                publication.processing[self.crossref_name] = ProcessingState(
                    status=ProcessingStatus.NOT_APPLICABLE, request_key="", attempts=self._next_attempt(state),
                    finished_at=datetime.now(UTC), result_count=0)
            else:
                try:
                    payload = crossref.get_work(publication.doi)
                    self.raw.append("crossref", payload, {"doi": publication.doi})
                    matches_count = 0
                    for author in payload.get("message", {}).get("author", []):
                        family = (author.get("family") or "").casefold()
                        orcid = (author.get("ORCID") or "").rstrip("/").split("/")[-1] or None
                        matches = [p for p in authors if p.name_raw and p.name_raw.split()[-1].casefold() == family]
                        if orcid and len(matches) == 1 and not matches[0].orcid:
                            matches[0].orcid = orcid
                            matches_count += 1
                    publication.processing[self.crossref_name] = ProcessingState(
                        status=ProcessingStatus.COMPLETED if matches_count else ProcessingStatus.COMPLETED_EMPTY,
                        request_key=key, attempts=self._next_attempt(state), finished_at=datetime.now(UTC),
                        result_count=matches_count)
                except Exception as exc:
                    publication.processing[self.crossref_name] = ProcessingState(
                        status=ProcessingStatus.FAILED, request_key=key, attempts=self._next_attempt(state),
                        finished_at=datetime.now(UTC), error=redact_text(exc))
            self.prepared.upsert_models("publications", [publication])
            self.prepared.upsert_models("persons", authors)
            crossref_changed += 1

        changed = 0
        openalex = OpenAlexClient(self.config.request_timeout, self.config.openalex_api_key)
        orcid = OrcidClient(self.config.request_timeout)
        for person in self.progress(eligible_people, total=len(eligible_people), label="Authors: OpenAlex and ORCID"):
            for source, request in ((self.openalex_name, openalex), (self.orcid_name, orcid)):
                # OpenAlex can supply an ORCID, so calculate the second key
                # after the first source has completed rather than up front.
                key = person.openalex_id or "" if source == self.openalex_name else person.orcid or ""
                state = person.processing.get(source)
                if not self._needs_source(state, key):
                    continue
                if not key:
                    person.processing[source] = ProcessingState(status=ProcessingStatus.NOT_APPLICABLE, request_key="",
                        attempts=self._next_attempt(state), finished_at=datetime.now(UTC), result_count=0)
                else:
                    try:
                        if source == self.openalex_name:
                            payload = request.get_author(key)
                            self.raw.append("openalex_authors", payload, {"author_id": key})
                            person.name_raw = payload.get("display_name") or person.name_raw
                            person.name_variants = list(dict.fromkeys([*person.name_variants,
                                                                       *(payload.get("display_name_alternatives") or [])]))
                            person.orcid = person.orcid or ((payload.get("orcid") or "").rstrip("/").split("/")[-1] or None)
                            person.affiliations = _merge_affiliations(person.affiliations, _openalex_affiliations(payload))
                        else:
                            payload = request.get_record(key)
                            self.raw.append("orcid", payload, {"orcid": key})
                            emails = (((payload.get("person") or {}).get("emails") or {}).get("email") or [])
                            # Every address ORCID lists is kept: a third of the
                            # people who list any list more than one, and the
                            # matcher recognises an account by whichever it used.
                            stated = [item["email"].strip().lower() for item in emails if item.get("email")]
                            person.emails = sorted(set(person.emails) | set(stated))
                            if not person.email and stated:
                                person.email = stated[0]
                            # Names the author publishes under besides the one
                            # ORCID shows: a credit name they prefer, and the
                            # spellings they registered themselves. A commit
                            # signed "Dangana, RS" is only recognisable through
                            # these.
                            person.other_names = _orcid_other_names(payload, person.other_names)
                            for field, url in _orcid_profiles(payload).items():
                                value = _github_login(url) if field == "github" else url
                                if value and not getattr(person, field):
                                    setattr(person, field, value)
                            person.affiliations = _merge_affiliations(person.affiliations, _orcid_affiliations(payload))
                        person.processing[source] = ProcessingState(status=ProcessingStatus.COMPLETED, request_key=key,
                            attempts=self._next_attempt(state), finished_at=datetime.now(UTC))
                    except Exception as exc:
                        person.processing[source] = ProcessingState(status=ProcessingStatus.FAILED, request_key=key,
                            attempts=self._next_attempt(state), finished_at=datetime.now(UTC), error=redact_text(exc))
                person.is_itmo = person.is_itmo or any(a.ror == ITMO_ROR_ID for a in person.affiliations)
                self._fill_missing_affiliations(person, publications)
                self._save_person(person)
                changed += 1

        openreview_found = openreview_changed = 0
        if self.config.openreview_username and self.config.openreview_password:
            client = OpenReviewClient(self.config.request_timeout, self.config.openreview_username,
                                      self.config.openreview_password)
            eligible_itmo = [person for person in eligible_people if person.is_itmo]
            by_email: dict[str, list[Person]] = {}
            for person in eligible_itmo:
                state = person.processing.get(self.openreview_name)
                if person.email and (self.force or state is None or state.phase in (None, "email")):
                    by_email.setdefault(person.email.casefold(), []).append(person)
                elif not person.email and (self.force or state is None):
                    person.processing[self.openreview_name] = ProcessingState(status=ProcessingStatus.NOT_STARTED,
                        phase=self._name_phase(person, publications), attempts=state.attempts if state else 0)
                    self._save_person(person)
            with self.progress_bar(total=sum(map(len, by_email.values())), label="OpenReview: email batches") as bar:
                emails = list(by_email)
                for start in range(0, len(emails), self.openreview_batch_size):
                    batch = emails[start:start + self.openreview_batch_size]
                    try:
                        payload = client.search_emails(batch)
                        self.raw.append("openreview", payload, {"method": "email_batch", "phase": "email", "emails": batch})
                        profiles = payload.get("profiles", [])
                        for email in batch:
                            profile = next((p for p in profiles if email in {str(p.get("email") or "").casefold(),
                                *(str(value).casefold() for value in ((p.get("content") or {}).get("emails") or [])),
                                *(str(value).casefold() for value in (p.get("confirmedEmails") or []))}), None)
                            for person in by_email[email]:
                                state = person.processing.get(self.openreview_name)
                                if profile:
                                    self._apply_profile(person, profile)
                                    person.processing[self.openreview_name] = ProcessingState(status=ProcessingStatus.COMPLETED,
                                        request_key=email, phase="email", attempts=self._next_attempt(state),
                                        finished_at=datetime.now(UTC), result_count=1)
                                    openreview_found += 1
                                else:
                                    person.processing[self.openreview_name] = ProcessingState(status=ProcessingStatus.NOT_STARTED,
                                        request_key=email, phase=self._name_phase(person, publications), attempts=self._next_attempt(state))
                                self._save_person(person)
                                openreview_changed += 1
                    except Exception as exc:
                        for email in batch:
                            for person in by_email[email]:
                                state = person.processing.get(self.openreview_name)
                                person.processing[self.openreview_name] = ProcessingState(status=ProcessingStatus.FAILED,
                                    request_key=email, phase="email", attempts=self._next_attempt(state),
                                    finished_at=datetime.now(UTC), error=redact_text(exc))
                                self._save_person(person)
                                openreview_changed += 1
                    bar.update(sum(len(by_email[email]) for email in batch))
            logger.info("OpenReview email: processed=%d, found=%d", openreview_changed, openreview_found)
            for phase in ("name_cs_ml", "name_remaining"):
                phase_changed, phase_found = self._run_name_searches(eligible_itmo, phase, client)
                openreview_changed += phase_changed
                openreview_found += phase_found
                logger.info("OpenReview %s: processed=%d, found=%d", phase, phase_changed, phase_found)
        return {"persons": changed + openreview_changed, "crossref": crossref_changed, "openreview": openreview_found}
