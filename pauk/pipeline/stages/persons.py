from datetime import datetime, timezone

from pauk.models import Person, Publication
from pauk.models.processing import ProcessingState, ProcessingStatus
from pauk.sources import OpenAlexClient
from pauk.sources.crossref import CrossrefClient
from pauk.sources.orcid import OrcidClient
from pauk.sources.openreview import OpenReviewClient

from .base import EnrichmentStage


RETRYABLE = {ProcessingStatus.NOT_STARTED, ProcessingStatus.FAILED}


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

    @staticmethod
    def _needs_attempt(state: ProcessingState | None) -> bool:
        return state is None or state.status in RETRYABLE

    def run(self) -> dict[str, int]:
        people = list(self.prepared.read_models("persons", Person))
        publications = {
            publication.id: publication
            for publication in self.prepared.read_models("publications", Publication)
        }
        eligible_people = self._people_in_scope(people)
        candidates = [
            person for person in eligible_people
            if self._needs_attempt(person.processing.get(self.name))
        ]
        by_publication: dict[str, list[Person]] = {}
        for person in eligible_people:
            for authored in person.authored:
                publication = publications.get(authored.publication_id)
                if publication and self._needs_attempt(publication.processing.get(self.crossref_name)):
                    by_publication.setdefault(authored.publication_id, []).append(person)

        crossref = CrossrefClient(self.config.request_timeout)
        crossref_changed = 0
        for publication_id, authors in by_publication.items():
            publication = publications.get(publication_id)
            if publication is None or not self._needs_attempt(publication.processing.get(self.crossref_name)):
                continue
            old_state = publication.processing.get(self.crossref_name)
            if not publication.doi:
                publication.processing[self.crossref_name] = ProcessingState(
                    status=ProcessingStatus.NOT_APPLICABLE,
                    attempts=(old_state.attempts if old_state else 0) + 1,
                    finished_at=datetime.now(timezone.utc), result_count=0,
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
                    finished_at=datetime.now(timezone.utc), result_count=matches_count,
                )
            except Exception as exc:
                publication.processing[self.crossref_name] = ProcessingState(
                    status=ProcessingStatus.FAILED,
                    attempts=(old_state.attempts if old_state else 0) + 1,
                    finished_at=datetime.now(timezone.utc), error=str(exc),
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
                if not person.openalex_id:
                    raise ValueError("missing OpenAlex author ID")
                before = person.model_dump(exclude={"processing", "authored", "contributed_to"})
                payload = client.get_author(person.openalex_id)
                self.raw.append("openalex_authors", payload, {"author_id": person.openalex_id})
                person.name_en = payload.get("display_name") or person.name_en
                person.name_variants = list(dict.fromkeys([
                    *person.name_variants, *(payload.get("display_name_alternatives") or []),
                ]))
                person.orcid = person.orcid or ((payload.get("orcid") or "").rstrip("/").split("/")[-1] or None)
                if person.orcid:
                    record = orcid_client.get_record(person.orcid)
                    self.raw.append("orcid", record, {"orcid": person.orcid})
                    emails = (((record.get("person") or {}).get("emails") or {}).get("email") or [])
                    if not person.email:
                        person.email = next((item.get("email") for item in emails if item.get("email")), None)
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
                    finished_at=datetime.now(timezone.utc), result_count=result_count,
                )
            except Exception as exc:
                person.processing[self.name] = ProcessingState(
                    status=ProcessingStatus.FAILED,
                    attempts=(state.attempts if state else 0) + 1,
                    finished_at=datetime.now(timezone.utc), error=str(exc),
                )
            changed += 1
        self.prepared.write_models("persons", people)
        self.prepared.write_models("publications", publications.values())
        return {"persons": changed, "crossref": crossref_changed}
