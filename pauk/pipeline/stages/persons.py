import re
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, CancelledError, ThreadPoolExecutor, wait
from contextlib import closing
from datetime import UTC, datetime
from itertools import batched
from time import monotonic

from pauk.models import Affiliation, Person, Publication
from pauk.models.processing import ProcessingState, ProcessingStatus
from pauk.pipeline.membership import (
    apply_membership,
    extend_owner_sources,
    normalize_ror,
    orcid_source_owner,
    refresh_orcid_membership,
)
from pauk.redaction import redact_text
from pauk.sources import OpenAlexClient
from pauk.sources.base import HttpRequestError
from pauk.sources.crossref import CrossrefClient
from pauk.sources.openalex import AUTHOR_BATCH_SIZE, normalize_author_id
from pauk.sources.orcid import OrcidAuthenticationError, OrcidClient

from .base import EnrichmentStage

PERSONS_POLL_SECONDS = 0.1


def _openalex_affiliations(payload: dict) -> list[Affiliation]:
    """Where the author's own OpenAlex record says they worked."""
    affiliations = []
    for entry in payload.get("affiliations") or []:
        institution = entry.get("institution") or {}
        if institution.get("display_name"):
            affiliations.append(Affiliation(
                name=institution["display_name"],
                ror=normalize_ror(institution.get("ror")),
                years=[int(year) for year in entry.get("years") or []],
                source="openalex",
            ))
    # last_known_institutions carries no years: it is the fallback for works
    # from a year no affiliation entry covers.
    for institution in payload.get("last_known_institutions") or []:
        if institution.get("display_name"):
            affiliations.append(Affiliation(
                name=institution["display_name"],
                ror=normalize_ror(institution.get("ror")),
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

    Authors paste the address bar as it stands, tab and all
    ("github.com/coralr-1?tab=repositories"), and a link to a repository
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
                ror = normalize_ror(disambiguated.get("disambiguated-organization-identifier"))
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
    merged: dict[tuple, Affiliation] = {}
    for affiliations in sources:
        for affiliation in affiliations:
            key = (affiliation.name, affiliation.source, affiliation.ror,
                   affiliation.source_person_id, affiliation.source_id)
            existing = merged.get(key)
            if existing is None:
                merged[key] = affiliation
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
    progress_label = "Authors: enriching profiles from OpenAlex and ORCID"
    crossref_name = "crossref"
    openalex_name = "openalex_author"
    orcid_name = "orcid"

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

    def _source_key(self, person: Person, source: str) -> str:
        return (person.openalex_id if source == self.openalex_name else person.orcid) or ""

    def _apply_payload(self, person: Person, source: str, key: str, payload: dict,
                       observed_at: datetime | None) -> None:
        """Parse source fields before changing the prepared person."""
        if source == self.openalex_name:
            name = payload.get("display_name")
            variants = payload.get("display_name_alternatives") or []
            if (name is not None and not isinstance(name, str)
                    or not isinstance(variants, list) or any(not isinstance(value, str) for value in variants)):
                raise ValueError("Invalid OpenAlex author name fields")
            orcid = person.orcid or ((payload.get("orcid") or "").rstrip("/").split("/")[-1] or None)
            affiliations = _openalex_affiliations(payload)
            person.name_raw = name or person.name_raw
            person.name_variants = list(dict.fromkeys([*person.name_variants, *variants]))
            person.orcid = orcid
            extend_owner_sources(person, orcid_source_owner(person, person.orcid or ""), orcid_id=person.orcid)
            person.affiliations = _merge_affiliations(person.affiliations, affiliations)
        else:
            emails = ((payload.get("person") or {}).get("emails") or {}).get("email") or []
            stated = [item["email"].strip().lower() for item in emails if item.get("email")]
            other_names = _orcid_other_names(payload, person.other_names)
            profiles = _orcid_profiles(payload)
            affiliations = _orcid_affiliations(payload)
            person.emails = sorted(set(person.emails) | set(stated))
            if not person.email and stated:
                person.email = stated[0]
            person.other_names = other_names
            for field, url in profiles.items():
                value = _github_login(url) if field == "github" else url
                if value and not getattr(person, field):
                    setattr(person, field, value)
                if (field == "github" and value and person.github_match
                        and person.github and value.casefold() == person.github.casefold()):
                    person.github_match.github = None
            refresh_orcid_membership(
                person, affiliations, orcid_source_owner(person, key), key,
                observed_at=observed_at,
            )

    def _complete_source(self, person: Person, source: str, publications: dict[str, Publication], *,
                         payload: dict | None = None, observed_at: datetime | None = None,
                         error: Exception | None = None) -> None:
        key = self._source_key(person, source)
        state = person.processing.get(source)
        if key and error is None:
            try:
                if not isinstance(payload, dict):
                    raise ValueError("Invalid author record response")
                self._apply_payload(person, source, key, payload, observed_at)
            except Exception as exc:
                error = exc
        status = (ProcessingStatus.NOT_APPLICABLE if not key else
                  ProcessingStatus.FAILED if error is not None else ProcessingStatus.COMPLETED)
        person.processing[source] = ProcessingState(
            status=status, request_key=key, attempts=self._next_attempt(state), finished_at=datetime.now(UTC),
            result_count=0 if not key else None, error=redact_text(error) if error is not None else None,
        )
        apply_membership(person)
        self._fill_missing_affiliations(person, publications)
        self._save_person(person)

    def _requests(self, source: str, jobs: list[tuple[str, ...]], heartbeat: Callable[[], None]
                  ) -> Iterator[tuple[tuple[str, ...], dict[str, dict] | None, Exception | None]]:
        """Keep a bounded request window; sessions belong to individual workers."""
        if not jobs:
            return
        stop = threading.Event()
        local = threading.local()
        clients = []
        lock = threading.Lock()

        def client():
            if stop.is_set():
                raise CancelledError
            if not hasattr(local, "client"):
                if source == self.openalex_name:
                    local.client = OpenAlexClient(self.config.request_timeout, self.config.openalex_api_key,
                                                  requests_per_second=self.config.openalex_rps, cancel=stop)
                else:
                    local.client = OrcidClient(self.config.request_timeout, self.config.orcid_client_id,
                                               self.config.orcid_client_secret,
                                               requests_per_second=self.config.orcid_rps, cancel=stop)
                with lock:
                    clients.append(local.client)
            return local.client

        def fetch(keys):
            request = client()
            if source == self.openalex_name:
                return request.get_authors(keys) if len(keys) > 1 else {keys[0]: request.get_author(keys[0])}
            return {keys[0]: request.get_record(keys[0])}

        def prepare():
            request = client()
            if source == self.orcid_name:
                request.authorize()

        pool = ThreadPoolExecutor(max_workers=self.config.persons_concurrency, thread_name_prefix="pauk-persons")
        pending = {}
        remaining = iter(jobs)

        def submit():
            if (keys := next(remaining, None)) is not None:
                pending[pool.submit(fetch, keys)] = keys

        try:
            startup = pool.submit(prepare)
            while not startup.done():
                heartbeat()
                wait((startup,), timeout=PERSONS_POLL_SECONDS)
            startup.result()
            for _ in range(min(self.config.persons_concurrency, len(jobs))):
                submit()
            while pending:
                heartbeat()
                ready, _ = wait(pending, timeout=PERSONS_POLL_SECONDS, return_when=FIRST_COMPLETED)
                for future in ready:
                    keys = pending.pop(future)
                    try:
                        records = future.result()
                    except (CancelledError, OrcidAuthenticationError):
                        raise
                    except Exception as error:
                        records, failure = None, error
                    else:
                        failure = None
                    submit()
                    yield keys, records, failure
        finally:
            stop.set()
            pool.shutdown(wait=True, cancel_futures=True)
            for request in clients:
                request.close()

    def _enrich_source(self, source: str, people: list[Person], publications: dict[str, Publication]) -> int:
        candidates = [person for person in people
                      if self._needs_source(person.processing.get(source), self._source_key(person, source))]
        openalex = source == self.openalex_name
        raw_source, request_key = ("openalex_authors", "author_id") if openalex else ("orcid", "orcid")
        groups: dict[str, list[Person]] = {}
        done, reported = 0, 0.0
        label = "Authors: OpenAlex" if openalex else "Authors: ORCID"

        def heartbeat():
            nonlocal reported
            if self.on_progress is not None and monotonic() - reported >= self.REPORT_SECONDS:
                reported = monotonic()
                self.on_progress(label, done, len(candidates))

        with self.progress_bar(total=len(candidates), label=label, unit="author") as bar:
            def complete(person, **kwargs):
                nonlocal done
                self._complete_source(person, source, publications, **kwargs)
                done += 1
                bar.update()
                heartbeat()

            for person in candidates:
                key = self._source_key(person, source)
                if not key:
                    complete(person)
                    continue
                try:
                    normalized = normalize_author_id(key) if openalex else key
                except ValueError as error:
                    complete(person, error=error)
                else:
                    groups.setdefault(normalized, []).append(person)
            size = AUTHOR_BATCH_SIZE if openalex else 1
            jobs = list(batched(groups, size))
            while jobs:
                fallbacks = []
                with closing(self._requests(source, jobs, heartbeat)) as responses:
                    for keys, records, error in responses:
                        if len(keys) > 1 and isinstance(error, HttpRequestError) and error.status_code == 400:
                            fallbacks.extend((key,) for key in keys)
                            continue
                        for key in keys:
                            if len(keys) > 1 and records is not None and key not in records:
                                fallbacks.append((key,))
                                continue
                            observed_at = None
                            payload = records.get(key) if records is not None else None
                            failure = error
                            if failure is None:
                                try:
                                    observed_at = self.raw.append(raw_source, payload, {request_key: key})
                                except Exception as exc:
                                    failure = exc
                            for person in groups[key]:
                                complete(person, payload=payload, observed_at=observed_at, error=failure)
                jobs = fallbacks
        return done

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
        crossref = CrossrefClient(self.config.request_timeout, self.config.crossref_mailto,
                                 requests_per_second=self.config.crossref_rps,
                                 max_concurrent=self.config.crossref_concurrency)
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
                            extend_owner_sources(matches[0], orcid_source_owner(matches[0], orcid), orcid_id=orcid)
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

        changed = self._enrich_source(self.openalex_name, eligible_people, publications)
        changed += self._enrich_source(self.orcid_name, eligible_people, publications)
        for person in self.progress(eligible_people, total=len(eligible_people), label="Authors: refreshing membership"):
            apply_membership(person)
            self._save_person(person)
        return {"persons": changed, "crossref": crossref_changed}
