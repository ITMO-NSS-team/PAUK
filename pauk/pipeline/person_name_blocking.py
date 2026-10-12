"""Role-aware name blocking, independent of the fitted resolver features.

Blocking only proposes comparisons. It never asserts that two records are
the same person, and exact identifiers bypass every name heuristic.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import lru_cache
from itertools import combinations

from pauk.models import Person
from pauk.pipeline.stages.author_names import _fold

_PARTICLES = frozenset({"de", "del", "della", "di", "da", "dos", "du", "la", "le", "van", "von", "der", "den"})
_DASHES = str.maketrans({char: "-" for char in "‐‑‒–—−"})
_TITLES = frozenset({"dr", "prof", "professor", "mr", "mrs", "ms"})
# Large collaborations cannot rescue otherwise incompatible personal names.
NAME_RESCUE_TEAM_LIMIT = 50


@lru_cache(maxsize=16384)
def name_tokens(value: str | None) -> tuple[str, ...]:
    """Keep compound boundaries; normalize accents, mixed scripts and initials."""
    value = unicodedata.normalize("NFKD", (value or "").translate(_DASHES))
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = re.sub(r"['’‘ʼ]", "", value)
    tokens = [token for token in re.findall(r"[^\W_]+(?:-[^\W_]+)*", _fold(value)) if any(c.isalpha() for c in token)]
    while tokens and tokens[0] in _TITLES:
        tokens.pop(0)
    return tuple(tokens)


@dataclass(frozen=True)
class NameForm:
    surname: str
    given: tuple[str, ...]
    explicit: bool = False
    contextual: bool = True


@dataclass
class _SurnameBlock:
    by_channel: dict[str, set[str]] = field(default_factory=dict)
    channels: dict[str, set[str]] = field(default_factory=dict)
    given: dict[str, set[tuple[str, ...]]] = field(default_factory=dict)

    def add(self, identifier: str, given: tuple[str, ...], channels: set[str]) -> None:
        self.given.setdefault(identifier, set()).add(given)
        self.channels.setdefault(identifier, set()).update(channels)
        for channel in channels:
            self.by_channel.setdefault(channel, set()).add(identifier)


def _surname_forms(
    tokens: tuple[str, ...], given: tuple[str, ...], explicit: bool, contextual: bool = True
) -> set[NameForm]:
    if not tokens:
        return set()
    whole = "".join(tokens).replace("-", "")
    result = {NameForm(whole, given, explicit, contextual)} if len(whole) > 1 else set()
    # Only a real separator licenses a shortened compound surname. A string
    # prefix (Ivanov / Ivanovich) is not a compound boundary.
    if any("-" in token for token in tokens):
        result.update(
            NameForm(part, given, True, contextual) for token in tokens for part in token.split("-") if len(part) > 2
        )
    if len(tokens) > 1 and all(token in _PARTICLES for token in tokens[:-1]):
        result.add(NameForm(tokens[-1], given, True, contextual))
    elif explicit and len(tokens) > 1:
        result.update(NameForm(token, given, True, contextual) for token in tokens if len(token) > 2)
    return result


def parse_name(value: str | None, surnames: tuple[str, ...] = ()) -> set[NameForm]:
    """Return possible roles without using a first name as a standalone key."""
    tokens = name_tokens(value)
    if not tokens:
        return set()
    if value and "," in value:
        left, right = value.split(",", 1)
        return _surname_forms(name_tokens(left), name_tokens(right), True)
    for surname in surnames:
        parts = name_tokens(surname)
        for start in range(len(tokens)):
            for end in range(start + 1, len(tokens) + 1):
                if "".join(tokens[start:end]).replace("-", "") == "".join(parts).replace("-", ""):
                    return _surname_forms(parts, tokens[:start] + tokens[end:], True)
    # A particle in one language can be a complete surname in another (Le,
    # Van). Treat it as a particle only when building a longer surname.
    long_indices = [i for i, token in enumerate(tokens) if len(token) > 1]
    if not long_indices:
        return set()
    if len(long_indices) == 1:
        index = long_indices[0]
        start = index
        while start > 0 and tokens[start - 1] in _PARTICLES:
            start -= 1
        return _surname_forms(tokens[start : index + 1], tokens[:start] + tokens[index + 1 :], True)
    result: set[NameForm] = set()
    start = long_indices[-1]
    while start > 0 and tokens[start - 1] in _PARTICLES:
        start -= 1
    result.update(_surname_forms(tokens[start:], tokens[:start], False))
    # Reversed unparsed names need corpus support before this alternative
    # is used. The residual given name must still agree with the other record.
    end = 1
    while end < len(tokens) and tokens[end - 1] in _PARTICLES:
        end += 1
    result.update(_surname_forms(tokens[:end], tokens[end:], False, contextual=False))
    if end > 1:
        result.update(_surname_forms(tokens[:1], tokens[1:], False, contextual=False))
    return result


def _given_initial(token: str) -> str:
    # Add an alias for Yevgeny / Evgeny without discarding the original initial.
    return "e" if token.startswith("ie") else token[:1]


@lru_cache(maxsize=16384)
def _compatible_given(first: tuple[str, ...], second: tuple[str, ...]) -> bool:
    if not first or not second:
        return True
    left, right = first[0], second[0]
    if left[0] == right[0] or _given_initial(left) == _given_initial(right):
        return True
    # Initial typos and spelling variants can change the first character.
    # This only admits a comparison; it must never become a merge rule.
    return min(len(left), len(right)) >= 3 and SequenceMatcher(None, left, right).ratio() >= 0.6


def _given_channels(given: tuple[str, ...]) -> set[str]:
    if not given:
        return {""}
    token = given[0]
    channels = {"initial:" + _given_initial(token), "initial:" + token[0]}
    if len(token) >= 3:
        channels.update("gram:" + token[i : i + 2] for i in range(len(token) - 1))
    return channels


def person_name_forms(people: list[Person]) -> dict[str, set[NameForm]]:
    forms_by_person: dict[str, set[NameForm]] = {}
    terminal_counts: Counter[str] = Counter()
    token_counts: Counter[str] = Counter()
    for person in people:
        surnames = tuple(value for value in (person.surname_ru, person.surname_en) if value)
        raw = parse_name(person.name_raw, surnames)
        given_fields = tuple(value for value in (person.first_name_ru, person.first_name_en) if value)
        # Parsed fields remain usable even if the raw display name is absent.
        for surname in surnames:
            surname_key = "".join(name_tokens(surname)).replace("-", "")
            if not given_fields and any(form.surname == surname_key for form in raw):
                continue
            for given in given_fields or ("",):
                raw.update(_surname_forms(name_tokens(surname), name_tokens(given), True))
        forms = set(raw)
        for name in (*person.name_variants, *person.other_names):
            for variant in parse_name(name, surnames):
                if not raw or any(_compatible_given(base.given, variant.given) for base in raw):
                    forms.add(variant)
        # Resolve roles from this record's own unambiguous spellings, not
        # merely because a common given name is somebody else's surname.
        supported = {form.surname for form in forms if form.explicit}
        if len(supported) == 1:
            forms = {form for form in forms if form.surname in supported or form.contextual}
        forms_by_person[person.id] = forms
        token_counts.update({form.surname for form in forms})
        terminals = set()
        for name in (person.name_raw, *person.name_variants, *person.other_names):
            parts = name_tokens(name)
            if parts:
                if name and "," in name:
                    terminals.update(form.surname for form in parse_name(name))
                else:
                    long = [token for token in parts if len(token) > 1]
                    if long:
                        terminals.add(long[-1].replace("-", ""))
        terminal_counts.update(terminals & {form.surname for form in forms})
    return {
        identifier: {
            form for form in forms if form.explicit or terminal_counts[form.surname] * 2 >= token_counts[form.surname]
        }
        for identifier, forms in forms_by_person.items()
    }


def _fuzzy_keys(surname: str) -> set[str]:
    if len(surname) < 5:
        return set()
    simplified = re.sub(r"(.)\1+", r"\1", surname).replace("ou", "u")
    return {simplified, *(simplified[:i] + simplified[i + 1 :] for i in range(len(simplified)))}


def _close_surnames(first: str, second: str) -> bool:
    return (
        min(len(first), len(second)) >= 5
        and first[0] == second[0]
        and SequenceMatcher(None, first, second).ratio() >= 0.8
    )


def conflicting_initials(first: Person, second: Person) -> bool:
    """Veto explicit initial contradictions even across close surname spellings.

    Missing initials and uncertain role assignments are not contradictions.
    Aliases cannot erase a contradiction in the two display names.
    """
    forms = []
    for person in (first, second):
        surnames = tuple(value for value in (person.surname_ru, person.surname_en) if value)
        forms.append(parse_name(person.name_raw, surnames))
    comparable = [
        (a, b)
        for a in forms[0]
        for b in forms[1]
        if (a.contextual or a.explicit)
        and (b.contextual or b.explicit)
        and (a.surname == b.surname or _close_surnames(a.surname, b.surname))
    ]

    def initial_options(token: str) -> set[str]:
        # A transliterated single letter can occupy several Latin letters.
        # Uncertain romanization must not become a deterministic veto.
        return {token[:1], _given_initial(token)} | {
            "iu": {"u", "y", "j"},
            "ia": {"a", "y", "j"},
            "ie": {"e", "y"},
            "kh": {"h", "k"},
            "zh": {"z", "j"},
            "sh": {"s"},
            "ch": {"c"},
        }.get(token, set())

    return bool(comparable) and all(
        any(
            (len(a) == 1 or len(b) == 1)
            and not (a.startswith(b) or b.startswith(a))
            and not initial_options(a) & initial_options(b)
            for a, b in zip(left.given, right.given, strict=False)
        )
        for left, right in comparable
    )


def candidate_person_pairs(
    people: list[Person],
    in_scope: set[str] | None,
    staff_ids: dict[str, str] | None = None,
) -> Iterator[tuple[Person, Person]]:
    """Index surname + given initial, retaining unresolved and exact-ID paths.

    A bucket is never silently dropped because it exceeds a size threshold.
    Genuinely indistinguishable homonyms still require quadratic comparisons;
    imposing a hard cap on those would sacrifice candidate recall.
    """
    by_id = {person.id: person for person in people}
    people = [by_id[identifier] for identifier in sorted(by_id)]
    forms = person_name_forms(people)
    works_by_person = {person.id: frozenset(entry.publication_id for entry in person.authored) for person in people}
    team_sizes = Counter(work for works in works_by_person.values() for work in works)
    rescue_works = {
        identifier: frozenset(work for work in works if team_sizes[work] <= NAME_RESCUE_TEAM_LIMIT)
        for identifier, works in works_by_person.items()
    }
    # Identical publication sets give identical coauthor evidence. Index each
    # set once instead of materializing every author-author edge of a large
    # collaboration. Single-work authors add nothing beyond the work channel.
    contexts_by_work: dict[str, set[str]] = defaultdict(set)
    work_sets = {works for works in rescue_works.values() if len(works) > 1}
    for index, works in enumerate(sorted(tuple(sorted(works)) for works in work_sets)):
        context_key = f"context:coauthor:{index}"
        for work in works:
            contexts_by_work[work].add(context_key)
    by_surname: dict[str, _SurnameBlock] = {}
    exact: dict[tuple[str, str], set[str]] = defaultdict(set)
    emitted: set[tuple[str, str]] = set()

    def emit(first: str, second: str):
        if first == second or (in_scope is not None and first not in in_scope and second not in in_scope):
            return
        pair = tuple(sorted((first, second)))
        if pair not in emitted:
            emitted.add(pair)
            yield by_id[pair[0]], by_id[pair[1]]

    for person in people:
        if person.orcid:
            orcid = person.orcid.strip().rstrip("/").rsplit("/", 1)[-1]
            if orcid:
                exact[("orcid", orcid)].add(person.id)
        if staff_id := (staff_ids or {}).get(person.id):
            exact[("staff", staff_id)].add(person.id)
        display_tokens = name_tokens(person.name_raw)
        if len(display_tokens) >= 2 and any(len(token) > 1 for token in display_tokens):
            exact[("full_name", " ".join(display_tokens))].add(person.id)
            exact[("joined_full_name", "".join(display_tokens).replace("-", ""))].add(person.id)
        # Context is a rescue channel for nicknames and language changes,
        # never a way to compare arbitrary people sharing a large department.
        context = {"context:work:" + work for work in rescue_works[person.id]}
        for work in rescue_works[person.id]:
            context.update(contexts_by_work[work])
        for form in sorted(forms[person.id], key=lambda form: (form.surname, form.given)):
            block = by_surname.setdefault(form.surname, _SurnameBlock())
            block.add(person.id, form.given, _given_channels(form.given) | (context if form.contextual else set()))

    for identifiers in exact.values():
        for first, second in combinations(sorted(identifiers), 2):
            yield from emit(first, second)

    def surname_pairs(first_surname: str, second_surname: str):
        left = by_surname[first_surname]
        right = by_surname[second_surname]
        for first, channels in left.channels.items():
            if "" in channels:
                candidates = set(right.given)
            else:
                candidates = set(right.by_channel.get("", ()))
                for channel in channels:
                    candidates.update(right.by_channel.get(channel, ()))
            if in_scope is not None and first not in in_scope:
                candidates.intersection_update(in_scope)
            # A pair often shares many publication/coauthor and spelling
            # keys. Union the postings before enumerating, not afterwards.
            for second in sorted(candidates):
                if first == second or (first_surname == second_surname and first > second):
                    continue
                if tuple(sorted((first, second))) in emitted:
                    continue
                context_match = any(key.startswith("context:") for key in channels & right.channels[second])
                if context_match or any(
                    _compatible_given(a, b) for a in left.given[first] for b in right.given[second]
                ):
                    yield from emit(first, second)

    for surname in sorted(by_surname):
        yield from surname_pairs(surname, surname)

    fuzzy_index: dict[str, set[str]] = defaultdict(set)
    for surname in by_surname:
        for key in sorted(_fuzzy_keys(surname)):
            fuzzy_index[key].add(surname)
    # Shared deletion keys only join similar spellings, never arbitrary
    # prefixes. Repeated keys cannot repeat the same surname comparison.
    compared: set[tuple[str, str]] = set()
    for spellings in fuzzy_index.values():
        for first, second in combinations(sorted(spellings), 2):
            key = (first, second)
            if key not in compared:
                compared.add(key)
                if _close_surnames(first, second):
                    yield from surname_pairs(first, second)
