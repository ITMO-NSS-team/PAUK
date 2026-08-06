from datetime import datetime, timezone

from pauk.models import Department, Person, Publication
from pauk.models.processing import ProcessingState, ProcessingStatus
from pauk.storage.static import StaticStore

from .base import EnrichmentStage


def _match_names(department: Department) -> list[str]:
    """Casefolded names to look for in affiliation text: English, Russian, variants.

    Matching stays plain substring containment; adding name_ru lets Cyrillic
    affiliations match, which name_en-only matching missed. Word-boundary matching
    was tried but measured net-negative on real affiliations — it dropped
    numbered ("2School of ...") and plural ("... Sciences" vs "Science") forms
    while removing no genuine false positives.
    """
    names = [department.name_en, department.name_ru, *department.name_variants]
    return [name.casefold() for name in names if name]


class DepartmentsStage(EnrichmentStage):
    name = "departments"

    def run(self) -> dict[str, int]:
        store = StaticStore(self.config.static_dir)
        departments = store.departments()
        schools = store.schools()
        matchers = [(d.id, _match_names(d)) for d in departments]
        people = list(self.prepared.read_models("persons", Person))
        publications = list(self.prepared.read_models("publications", Publication))
        by_pub = {p.id: p for p in publications}
        changed = 0
        for person in people:
            if self.selection is not None:
                if self.selection.entity == "persons" and person.id not in self.selection.ids:
                    continue
                if self.selection.entity == "publications" and not any(
                    authorship.publication_id in self.selection.ids for authorship in person.authored
                ):
                    continue
                if self.selection.entity not in {"persons", "publications"}:
                    continue
            state = person.processing.get(self.name)
            if not self.needs_attempt(state):
                continue
            text = " ".join(a.affiliation or "" for a in person.authored).casefold()
            matched = [dept_id for dept_id, names in matchers if any(name in text for name in names)]
            person.department_ids = list(dict.fromkeys([*person.department_ids, *matched]))
            for authorship in person.authored:
                pub = by_pub.get(authorship.publication_id)
                if pub and person.is_itmo:
                    pub.department_ids = list(dict.fromkeys([*pub.department_ids, *matched]))
            person.processing[self.name] = ProcessingState(
                status=ProcessingStatus.COMPLETED if matched else ProcessingStatus.COMPLETED_EMPTY,
                attempts=(state.attempts if state else 0) + 1,
                finished_at=datetime.now(timezone.utc),
                result_count=len(matched),
            )
            changed += 1
        self.prepared.write_models("persons", people)
        self.prepared.write_models("departments", departments)
        self.prepared.write_models("schools", schools)
        self.prepared.write_models("publications", publications)
        return {"persons": changed, "departments": len(departments), "schools": len(schools)}
