import html
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache

from pauk.institution import Institution
from pauk.models import Department, Person, Publication
from pauk.models.processing import ProcessingState, ProcessingStatus
from pauk.storage.static import StaticStore

from .base import EnrichmentStage

logger = logging.getLogger(__name__)

# Affiliations read "<department>, <organisation>, <address>", one per segment
# between these separators; splitting a segment on commas yields those parts, so
# the university's marker can be located next to a name.
_SEGMENT_SPLIT = re.compile(r"[\n;]")
# Words that name an organisation of their own in any language the profiles
# meet; each profile adds its neighbours' abbreviations.
_OTHER_ORGANISATION = (
    r"universi(?:t|dad)|üniversite|uniwersytet|\buniv\b|университет|academ(?:y|ies|ia)\b|академи(?:я|и|ю|ей)\b"
    r"|college|hospital"
)
_SUB_UNIT = re.compile(
    r"\(?\d*\)?\s*(?:the\s+)?(?:(?:international|joint|research|scientific|educational|and|&|"
    r"международн\w*|научн\w*|образовательн\w*|исследовательск\w*)[\s-]+)*"
    r"(?:lab|laborator(?:y|ies)|department|dept|faculty|chair|кафедр\w*|лаборатор\w*|факультет\w*|отдел\w*"
    r"|departamento|depto|faculdade|laborat[óo]rio|n[úu]cleo|programa|grupo\s+de\s+pesquisa"
    r"|school|graduate\s+school|college|division|lehrstuhl|fakult[äa]t|abteilung|professur|institut\s+f[üu]r"
    r"|d[ée]partement|facult[ée]|laboratoire|[ée]cole|unit[ée])\b"
    r"|.*\blab$",
    re.IGNORECASE,
)
# Address words every profile shares; the city and street names are the profile's.
_PLACE_WORD = r"av|ave|avenue|str|street|bldg|building|build|the|a"
_TITLE = re.compile(
    r"(?:candidate|doctor) of (?:[\w.-]+ ){0,3}sciences?|(?:associate |assistant |full )?professor|assistant"
    r"|(?:senior |chief |leading |junior |principal )?research(?:er| fellow| scientist| engineer| associate)"
    r"|(?:senior )?lecturer|engineer|scientist|dean|postgraduate|ph\.? ?d\.? candidate"
    r"|(?:ph\.? ?d\.? |graduate |master'?s? |bachelor'?s? |msc |undergraduate |doctoral )?student"
    r"|ph\.? ?d\.?|(?:d|cand|dr)\.? ?sc(?:i|\.)?\.?(?: ?\([^()]{0,20}\))?|postdoc\w*"
    r"|(?:кандидат|доктор) (?:[\w.-]+ ){0,3}наук|[кд]\.? ?[тфхб]\.? ?н\.?|доцент|профессор|ассистент"
    r"|(?:старший )?преподаватель|(?:старший |ведущий |главный |младший )?научный сотрудник|инженер|аспирант"
    r"|магистрант|студент"
    r"|professora|pesquisador(?:a)?|p[óo]s-?doutorand[oa]|doutorand[oa]|mestrand[oa]|alun[oa]|estudante|bolsista"
    r"|(?:doutor|mestre)a? em (?:[\w.-]+ ){0,3}",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _Context:
    """The patterns that tell one university's parts apart from its neighbours'."""

    marker: re.Pattern[str]
    long_name: re.Pattern[str]
    replacement: str
    other_organisation: re.Pattern[str]
    place_word: re.Pattern[str]
    unit_words: re.Pattern[str] | None

    def is_other_organisation(self, part: str) -> bool:
        return bool(self.other_organisation.search(part)) and not (self.unit_words and self.unit_words.search(part))


@cache
def _context(institution: Institution) -> _Context:
    return _Context(
        marker=institution.marker_re,
        long_name=institution.long_name_re,
        replacement=institution.marker_replacement,
        other_organisation=re.compile(f"{_OTHER_ORGANISATION}|{institution.other_organisations}", re.IGNORECASE),
        place_word=re.compile(f"{_PLACE_WORD}|{institution.place_words}"),
        unit_words=institution.unit_words_re,
    )


# The catalogue quotes and hyphenates a name ("Energy-Efficient"), authors often
# do neither; folding both to spaces on each side lets containment survive that.
_PUNCT = re.compile("[«»“”„‟‘’‚‛\"'‐-―-]")
_APOSTROPHE = re.compile(r"(?<=[^\W\d_])['’‘´ʼʹ`�](?=[^\W\d_])")


def _normalize(text: str) -> str:
    """Casefold, drop apostrophes inside words, fold quotes and dashes to spaces, collapse whitespace."""
    return " ".join(_PUNCT.sub(" ", _APOSTROPHE.sub("", text.casefold())).split())


def _context_names(department: Department) -> list[str]:
    """Normalized generic aliases matched only next to the university's marker.

    Names like "Department of Physics" also name foreign units, so matching them
    against the whole affiliation blob would pull in co-affiliations. Requiring the
    marker close by (see _member_context_parts) recovers the member authors
    without that cost.
    """
    return [_normalize(name) for name in department.context_aliases if name]


def _match_names(department: Department) -> list[str]:
    """Normalized names to look for in affiliation text: English, local language, variants.

    A name also listed among the context aliases is left to them, so a unit with a
    generic official name ("Institute of Mathematics") matches only next to the marker.
    """
    context = set(_context_names(department))
    names = [department.name_en, department.name_local, *department.name_variants]
    return [normalized for name in names if name and (normalized := _normalize(name)) not in context]


@cache
def _name_pattern(name: str) -> re.Pattern[str]:
    tail = r"(?![^\W\d_])" if len(name.rsplit(" ", 1)[-1]) <= 2 or " " not in name else ""
    return re.compile(rf"(?<![^\W\d_]){re.escape(name)}{tail}")


def _contains(name: str, text: str) -> bool:
    """Whether name starts a word in text; a one-word name, or one ending in a short word, must also end one.

    "Megafaculty" does not hold "Faculty", "MorFBITs" does not hold FBIT, "Институт ИИТ" does not hold
    "Институт ИИ"; "2School of" still matches.
    """
    return bool(name) and name in text and _name_pattern(name).search(text) is not None


def _says_nothing(part: str, ctx: _Context) -> bool:
    """Whether a part holds nothing but academic titles and the university's address."""
    words = re.findall(r"[^\W\d_]+", _normalize(_TITLE.sub(" ", part)))
    return all(ctx.place_word.fullmatch(word) for word in words)


def _bridges(part: str, unit_names: list[str]) -> bool:
    """Whether a part keeps a name and the marker together: it is a sub-unit or opens with a catalogue name."""
    if _SUB_UNIT.match(part):
        return True
    normalized = re.sub(r"^(?:\d+\s*|the\s+)", "", _normalize(part))
    return any(_name_pattern(name).match(normalized) for name in unit_names)


def _organisation_is_member(segment: list[str], i: int, ctx: _Context) -> bool:
    """Whether part i of a segment is not owned by another organisation: none is named before the next marker."""
    for k in range(i, len(segment)):
        if ctx.marker.search(segment[k]):
            return True
        if ctx.is_other_organisation(segment[k]):
            return False
    return True


def _near_member(
    segments: list[list[str]], at: list[tuple[int, int]], i: int, j: int, unit_names: list[str], ctx: _Context
) -> bool:
    """Whether part i may borrow the marker of part j; at maps a part number to its segment and position.

    The same or a neighbouring part always may, one part further only across a part that _bridges and names no
    other organisation. A marker in another segment counts only if the rest of i's segment bridges too, and
    never when another organisation owns part i.
    """
    (s, pos), (marker_s, _) = at[i], at[j]
    if abs(i - j) == 2:
        middle_s, middle_pos = at[min(i, j) + 1]
        middle = segments[middle_s][middle_pos]
        if not _bridges(middle, unit_names) or ctx.is_other_organisation(middle):
            return False
    if s != marker_s and not all(_bridges(part, unit_names) for k, part in enumerate(segments[s]) if k != pos):
        return False
    return _organisation_is_member(segments[s], pos, ctx)


def _member_context_parts(affiliation: str, unit_names: list[str], ctx: _Context) -> list[str]:
    """Normalized parts of one affiliation that sit next to the university's marker.

    Parts that say nothing are left out before distances are counted.
    """
    raw = _SEGMENT_SPLIT.split(ctx.long_name.sub(ctx.replacement, affiliation))
    segments = [
        [part.strip() for part in segment.split(",") if not _says_nothing(part.strip(), ctx)] for segment in raw
    ]
    at = [(s, pos) for s, segment in enumerate(segments) for pos in range(len(segment))]
    marked = {i for i, (s, pos) in enumerate(at) if ctx.marker.search(segments[s][pos])}
    return [
        _normalize(segments[s][pos])
        for i, (s, pos) in enumerate(at)
        if any(j in marked and _near_member(segments, at, i, j, unit_names, ctx) for j in range(i - 2, i + 3))
    ]


def _outlives_longer_names(name: str, text: str, names: set[str]) -> bool:
    """Whether name occurs in text outside every occurrence of a longer co-matching name containing it."""
    longer = [other for other in names if other != name and name in other]
    if not longer:
        return True
    covered = [(m.start(), m.end()) for other in longer for m in re.finditer(re.escape(other), text)]
    return any(
        not any(start <= m.start() and m.end() <= end for start, end in covered)
        for m in _name_pattern(name).finditer(text)
    )


def _match_person(
    affiliations: list[str],
    matchers: list[tuple[str, list[str]]],
    ctx_matchers: list[tuple[str, list[str]]],
    institution: Institution,
) -> list[str]:
    """Department ids named in one person's affiliations; a name inside a longer matched name does not count."""
    text = _normalize(" ".join(affiliations))
    plain = [(dept_id, name) for dept_id, names in matchers for name in names if _contains(name, text)]
    unit_names = [name for _, names in [*matchers, *ctx_matchers] for name in names]
    parts = (
        [
            part
            for affiliation in affiliations
            for part in _member_context_parts(affiliation, unit_names, _context(institution))
        ]
        if ctx_matchers
        else []
    )
    context = [
        (dept_id, name, part)
        for part in parts
        for dept_id, names in ctx_matchers
        for name in names
        if _contains(name, part)
    ]
    names = {name for _, name in plain} | {name for _, name, _ in context}
    kept = [dept_id for dept_id, name in plain if _outlives_longer_names(name, text, names)]
    kept += [dept_id for dept_id, name, part in context if _outlives_longer_names(name, part, names)]
    return list(dict.fromkeys(kept))


class DepartmentsStage(EnrichmentStage):
    name = "departments"
    progress_label = "Authors: matching affiliations with the university's departments"

    def run(self) -> dict[str, int]:
        store = StaticStore(self.config.static_dir)
        departments = store.departments()
        organizations = store.organizations()
        # Root organisations (the university, co-affiliations) are separate Organization
        # nodes, never in `departments`, so every unit here is matchable. Matching
        # the org name itself is undesirable anyway (it is in almost every affiliation).
        matchers = [(d.id, _match_names(d)) for d in departments]
        ctx_matchers = [(d.id, _context_names(d)) for d in departments if d.context_aliases]
        people = list(self.prepared.read_models("persons", Person))
        publications = list(self.prepared.read_models("publications", Publication))
        by_pub = {p.id: p for p in publications}
        candidates = [
            person
            for person in people
            if self._person_in_scope(person) and self.needs_attempt(person.processing.get(self.name))
        ]
        changed = 0
        seen: set[str] = set()
        for person in self.progress(candidates, total=len(candidates)):
            state = person.processing.get(self.name)
            affiliations = [html.unescape(a.affiliation or "") for a in person.authored]
            matched = _match_person(affiliations, matchers, ctx_matchers, self.config.institution)
            seen.update(matched)
            person.department_ids = list(dict.fromkeys([*person.department_ids, *matched]))
            for authorship in person.authored:
                pub = by_pub.get(authorship.publication_id)
                if pub and person.is_member:
                    pub.department_ids = list(dict.fromkeys([*pub.department_ids, *matched]))
            person.processing[self.name] = ProcessingState(
                status=ProcessingStatus.COMPLETED if matched else ProcessingStatus.COMPLETED_EMPTY,
                attempts=(state.attempts if state else 0) + 1,
                finished_at=datetime.now(UTC),
                result_count=len(matched),
            )
            changed += 1
        # Only a pass that looked at every author says anything about catalogue
        # coverage. A scoped run, or a resumed one where most rows are already
        # completed, sees a handful of people, and the same line would read as
        # though coverage had collapsed.
        if len(candidates) == len(people):
            logger.info("Matched %d of %d catalogue departments", len(seen), len(departments))
            members = [p for p in people if p.is_member]
            logger.info(
                "Matched %d of %d member authors to a department",
                sum(1 for p in members if p.department_ids),
                len(members),
            )
        self.prepared.write_models("persons", people)
        self.prepared.write_models("departments", departments)
        self.prepared.write_models("organizations", organizations)
        self.prepared.write_models("publications", publications)
        return {"persons": changed, "departments": len(departments), "organizations": len(organizations)}

    def _person_in_scope(self, person: Person) -> bool:
        if self.selection is None:
            return True
        if self.selection.entity == "persons":
            return person.id in self.selection.ids
        if self.selection.entity == "publications":
            return any(authorship.publication_id in self.selection.ids for authorship in person.authored)
        return False
