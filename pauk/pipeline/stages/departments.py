import re
from datetime import datetime, timezone

from pauk.models import Department, Person, Publication
from pauk.models.processing import ProcessingState, ProcessingStatus
from pauk.storage.static import StaticStore

from .base import EnrichmentStage

# Affiliations read "<department>, <organisation>, <address>"; splitting on these
# separators yields those parts, so an ITMO marker can be located next to a name.
_PART_SPLIT = re.compile(r"[\n;,]")
# A part carrying this marker is trusted ITMO context. A generic context_alias is
# accepted only when such a marker sits in its own or an adjacent part (i.e. the
# organisation right beside the department), never merely elsewhere in the blob —
# that is what keeps a co-affiliated "Department of Physics, SPbU" from matching.
_ITMO_MARKER = re.compile(r"\bitmo\b|\bifmo\b|итмо|information technolog\w*,?\s*mechanics", re.IGNORECASE)


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


def _context_names(department: Department) -> list[str]:
    """Casefolded generic aliases matched only next to an ITMO marker.

    Names like "Department of Physics" also name foreign units, so matching them
    against the whole affiliation blob would pull in co-affiliations. Requiring an
    ITMO marker in the same or an adjacent part recovers the ITMO authors without
    that cost.
    """
    return [name.casefold() for name in department.context_aliases if name]


class DepartmentsStage(EnrichmentStage):
    name = "departments"

    def run(self) -> dict[str, int]:
        store = StaticStore(self.config.static_dir)
        departments = store.departments()
        schools = store.schools()
        matchers = [(d.id, _match_names(d)) for d in departments]
        ctx_matchers = [(d.id, _context_names(d)) for d in departments if d.context_aliases]
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
            affiliations = [a.affiliation or "" for a in person.authored]
            text = " ".join(affiliations).casefold()
            matched = [dept_id for dept_id, names in matchers if any(name in text for name in names)]
            # ITMO-context pass: generic aliases match only in a part adjacent to an
            # ITMO marker, so a co-affiliated foreign department cannot pull them in.
            if ctx_matchers:
                itmo_parts: list[str] = []
                for affiliation in affiliations:
                    parts = _PART_SPLIT.split(affiliation)
                    marked = {i for i, part in enumerate(parts) if _ITMO_MARKER.search(part)}
                    if not marked:
                        continue
                    itmo_parts += [part.casefold() for i, part in enumerate(parts) if marked & {i - 1, i, i + 1}]
                for part in itmo_parts:
                    hits = [(dept_id, name) for dept_id, names in ctx_matchers for name in names if name in part]
                    # Keep the most specific alias per part: drop one that is merely a
                    # substring of a longer co-matching alias ("Department of Physics"
                    # inside "Department of Physics and Engineering").
                    matched += [
                        dept_id
                        for dept_id, name in hits
                        if not any(name != other and name in other for _, other in hits)
                    ]
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
