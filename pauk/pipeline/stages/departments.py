from datetime import UTC, datetime

from pauk.models import Person, Publication
from pauk.models.processing import ProcessingState, ProcessingStatus
from pauk.storage.static import StaticStore

from .base import EnrichmentStage


class DepartmentsStage(EnrichmentStage):
    name = "departments"
    progress_label = "Authors: matching affiliations with ITMO departments"

    def run(self) -> dict[str, int]:
        departments = StaticStore(self.config.static_dir).departments()
        people = list(self.prepared.read_models("persons", Person))
        publications = list(self.prepared.read_models("publications", Publication))
        by_pub = {p.id: p for p in publications}
        candidates = [
            person for person in people
            if self._person_in_scope(person) and self.needs_attempt(person.processing.get(self.name))
        ]
        changed = 0
        for person in self.progress(candidates, total=len(candidates)):
            state = person.processing.get(self.name)
            text = " ".join(a.affiliation or "" for a in person.authored).casefold()
            matched = [d.id for d in departments if d.name_en.casefold() in text or any(v.casefold() in text for v in d.name_variants)]
            person.department_ids = list(dict.fromkeys([*person.department_ids, *matched]))
            for authorship in person.authored:
                pub = by_pub.get(authorship.publication_id)
                if pub and person.is_itmo:
                    pub.department_ids = list(dict.fromkeys([*pub.department_ids, *matched]))
            person.processing[self.name] = ProcessingState(
                status=ProcessingStatus.COMPLETED if matched else ProcessingStatus.COMPLETED_EMPTY,
                attempts=(state.attempts if state else 0) + 1,
                finished_at=datetime.now(UTC), result_count=len(matched),
            )
            changed += 1
        self.prepared.write_models("persons", people)
        self.prepared.write_models("departments", departments)
        self.prepared.write_models("publications", publications)
        return {"persons": changed, "departments": len(departments)}

    def _person_in_scope(self, person: Person) -> bool:
        if self.selection is None:
            return True
        if self.selection.entity == "persons":
            return person.id in self.selection.ids
        if self.selection.entity == "publications":
            return any(authorship.publication_id in self.selection.ids for authorship in person.authored)
        return False
