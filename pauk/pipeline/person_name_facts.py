"""Deterministic name observations for both model views, never merge rules."""

from __future__ import annotations

from difflib import SequenceMatcher
from itertools import zip_longest

from pauk.models import Person
from pauk.pipeline.person_name_blocking import name_tokens, parse_name

_INITIAL_ALIASES = {
    "iu": {"i", "u", "y", "j"},
    "ia": {"i", "a", "y", "j"},
    "ie": {"i", "e", "y"},
    "kh": {"k", "h"},
    "zh": {"z", "j"},
    "sh": {"s"},
    "ch": {"c"},
}


def _component_relation(first: str | None, second: str | None) -> str:
    if first is None or second is None:
        return "missing_on_one_side"
    if first == second:
        return "equal_after_normalization"
    if len(first) == 1 or len(second) == 1 or first in _INITIAL_ALIASES or second in _INITIAL_ALIASES:
        initial_a = _INITIAL_ALIASES.get(first, {first[0]})
        initial_b = _INITIAL_ALIASES.get(second, {second[0]})
        return "compatible_initial" if initial_a & initial_b else "different_initials"
    if SequenceMatcher(None, first, second).ratio() >= 0.8:
        return "possible_spelling_variant"
    return "different_full_tokens"


def name_comparison_facts(first: Person, second: Person) -> dict:
    """Expose plausible primary-name alignments without promoting aliases to truth.

    Role ambiguity stays visible. Similarity and compatible initials describe
    spelling only; neither establishes that the records identify one person.
    """
    parsed = [
        parse_name(person.name_raw, tuple(value for value in (person.surname_ru, person.surname_en) if value))
        for person in (first, second)
    ]
    alternatives = []
    for left in parsed[0]:
        for right in parsed[1]:
            if left.surname == right.surname:
                surname_relation = "equal_after_normalization"
            elif (
                min(len(left.surname), len(right.surname)) >= 5
                and SequenceMatcher(None, left.surname, right.surname).ratio() >= 0.8
            ):
                surname_relation = "possible_spelling_variant"
            else:
                continue
            components = [
                {"a": a, "b": b, "relation": _component_relation(a, b)}
                for a, b in zip_longest(left.given, right.given)
            ]
            alternatives.append(
                {
                    "surname_a": left.surname,
                    "surname_b": right.surname,
                    "surname_relation": surname_relation,
                    "given_components": components,
                    "role_assignment": "explicit" if left.explicit and right.explicit else "inferred",
                }
            )
    # Prefer exact surname observations, then fewer actual spelling conflicts.
    # The remaining deterministic tie-breakers make prompts/cache keys stable.
    alternatives.sort(
        key=lambda row: (
            row["surname_relation"] != "equal_after_normalization",
            sum(c["relation"] in {"different_initials", "different_full_tokens"} for c in row["given_components"]),
            row["surname_a"],
            row["surname_b"],
            tuple((c["a"] or "", c["b"] or "") for c in row["given_components"]),
        )
    )
    return {
        "normalized_tokens_a": list(name_tokens(first.name_raw)),
        "normalized_tokens_b": list(name_tokens(second.name_raw)),
        "alignments": alternatives[:3],
        "alignment_count": len(alternatives),
        "meaning": "spelling_observations_not_identity_proof",
    }
