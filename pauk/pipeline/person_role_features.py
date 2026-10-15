"""Role-aligned feature contract shared by fitting and inference."""

from __future__ import annotations

import math
from collections.abc import Mapping
from difflib import SequenceMatcher
from itertools import zip_longest

from pauk.pipeline.person_name_blocking import name_tokens, parse_name
from pauk.pipeline.person_name_facts import _component_relation


def primary_forms(name):
    forms = parse_name(name)
    return {f for f in forms if f.explicit} or {f for f in forms if f.contextual}


def alignment(first, second):
    left, right = parse_name(first), parse_name(second)
    explicit_left, explicit_right = (
        any(f.explicit for f in left),
        any(f.explicit for f in right),
    )
    reorder = bool(name_tokens(first)) and sorted(name_tokens(first)) == sorted(name_tokens(second))
    # A known surname can anchor a reversed name. Otherwise, sharing a first
    # name must not turn that token into an invented common surname.
    if not explicit_right and not reorder:
        left = primary_forms(first)
    if not explicit_left and not reorder:
        right = primary_forms(second)
    options = []
    for a in left:
        for b in right:
            similarity = SequenceMatcher(None, a.surname, b.surname).ratio()
            relations = [_component_relation(x, y) for x, y in zip_longest(a.given, b.given)]
            conflicts = sum(r in {"different_full_tokens", "different_initials"} for r in relations)
            tie = tuple(sorted(((a.surname, a.given), (b.surname, b.given))))
            options.append(((-similarity, conflicts, tie), a, b, relations, similarity))
    if not options:
        return None
    return min(options, key=lambda x: x[0])


def role_features(item, values: dict[str, float], counts: Mapping[str, int]) -> dict[str, float]:
    match = alignment(item.name_a, item.name_b)
    if match:
        _, a, b, relations, similarity = match
        same = a.surname == b.surname
        observed = [(x, y) for x, y in zip_longest(a.given, b.given) if x and y]
        initial = bool(a.given and b.given) and (
            a.given[0][0] == b.given[0][0] or relations[0] == "compatible_initial"
        )
        rarity = 1 / math.log2(2 + counts.get(a.surname, 0) + counts.get(b.surname, 0))
        extras = {
            "surname_similarity": similarity,
            "given_full_conflicts": float(relations.count("different_full_tokens")),
            "given_initial_conflicts": float(relations.count("different_initials")),
            "given_coverage": len(observed) / max(1, len(relations)),
            "roles_explicit": float(a.explicit and b.explicit),
            "roles_inferred": float(not (a.explicit and b.explicit)),
            "roles_missing": 0.0,
        }
    else:
        same, initial, rarity = False, False, 0.0
        extras = dict.fromkeys(
            (
                "surname_similarity",
                "given_full_conflicts",
                "given_initial_conflicts",
                "given_coverage",
                "roles_explicit",
                "roles_inferred",
            ),
            0.0,
        )
        extras["roles_missing"] = 1.0
    values.update(
        surname_equal=float(same),
        first_initial_equal=float(initial),
        surname_rarity=rarity,
    )
    strong = float(values["same_tokens"] == 1 or (same and values["name_similarity"] >= 0.82))
    values["strong_name"] = strong
    for name, factor in {
        "orcid_x_name": values["same_orcid"],
        "name_x_coauthors": values["shared_coauthors"],
        "fallback_x_name": min(values["fallback_count"], 1),
        "rare_x_name": rarity,
        "name_x_department": min(values["shared_departments"], 1),
        "name_x_joint_work": min(values["joint_works"], 1),
    }.items():
        values[name] = strong * factor
    values["rare_compatible_name"] = values["compatible_name"] * rarity
    values["evidence_families"] = float(
        sum(
            (
                bool(strong),
                bool(values["same_orcid"] or values["same_staff"]),
                item.shared_coauthors > 0,
                item.shared_departments > 0,
                item.shared_fields >= 2,
                values["fallback_count"] > 0,
                item.shared_publications > 0,
            )
        )
    )
    return {"roles_v2__" + key: float(value) for key, value in (values | extras).items()}
