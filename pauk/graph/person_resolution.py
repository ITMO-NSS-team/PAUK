"""Pure inference for the experimental duplicate-person resolver.

Candidate generation, component conflict checks and graph writes stay in
``pauk.graph.dedup``. This module only turns evidence about one pair into a
decision, so it can be benchmarked before it is connected to Neo4j.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import StrEnum

FIRST_STAGE_SYSTEM_PROMPT = """You resolve duplicate researcher records for a scholarly database.
Decide whether both records denote the same individual. Use only supplied evidence. Names are evidence,
not proof; handle initials, patronymics, transliteration, token order and spelling variants. A shared
publication alone can mean two distinct coauthors. Missing identifiers or graph overlap are neutral.
Conflicting nonempty ORCID, staff identity or profile identifiers forbid merging. Return exactly JSON:
{"duplicate": boolean, "confidence": number from 0.5 to 1, "reason": "max 20 words"}."""

SECOND_STAGE_SYSTEM_PROMPT = """You are an independent second-stage identity adjudicator.
The pair was proposed as a duplicate by another model, but that proposal is not evidence. Decide whether
both records denote the same researcher using only supplied names, identifiers, aliases, coauthors,
departments, research fields and publication years. A matching name alone is insufficient for common
surnames or initials-only names. Conflicting full names, patronymics, ORCID, staff identity or profile
identifiers mean different people. Return exactly JSON: {"duplicate": boolean, "confidence": number from
0.5 to 1, "reason": "max 24 words"}."""

_CYRILLIC_TO_LATIN = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
})


class Decision(StrEnum):
    MERGE = "merge"
    SEPARATE = "separate"
    FIRST_MODEL = "first_model"
    SECOND_MODEL = "second_model"


@dataclass(frozen=True)
class ResolverPolicy:
    """Confidence zones selected for one reproducible evaluation profile."""

    separate_below: float = 0.05
    merge_from: float = 0.99

    def __post_init__(self) -> None:
        if not 0 <= self.separate_below < self.merge_from <= 1:
            raise ValueError("expected 0 <= separate_below < merge_from <= 1")


DEFAULT_POLICY = ResolverPolicy()


@dataclass(frozen=True)
class PairEvidence:
    person_a: str
    name_a: str
    person_b: str
    name_b: str
    orcid_a: str | None = None
    orcid_b: str | None = None
    staff_id_a: str | None = None
    staff_id_b: str | None = None
    catalog_status_a: str = ""
    catalog_status_b: str = ""
    shared_coauthors: int = 0
    shared_departments: int = 0
    shared_fields: int = 0
    shared_publications: int = 0
    works_a: int = 0
    works_b: int = 0
    surname_occurrences_a: int = 0
    surname_occurrences_b: int = 0
    profile_conflict: bool = False
    initials_conflict: bool = False


@dataclass(frozen=True)
class ModelVerdict:
    duplicate: bool
    confidence: float
    reason: str = ""

    def __post_init__(self) -> None:
        if type(self.duplicate) is not bool:
            raise ValueError("duplicate must be boolean")
        if isinstance(self.confidence, bool) or not 0.5 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0.5 and 1")


@dataclass(frozen=True)
class Resolution:
    decision: Decision
    route: str
    probability: float
    reason: str = ""


MODEL_FEATURES = (
    "name_similarity", "token_jaccard", "exact_name", "same_tokens", "surname_equal",
    "first_initial_equal", "initial_count", "full_token_min", "same_orcid", "orcid_conflict",
    "one_orcid", "same_staff", "staff_conflict", "trusted_catalog_count", "display_only_count",
    "fallback_count", "shared_coauthors", "shared_departments", "shared_fields", "joint_works",
    "works_min", "works_ratio", "surname_rarity", "name_corroboration", "strong_name",
    "initials_only_pair", "orcid_x_name", "name_x_coauthors", "initials_x_coauthors",
    "fallback_x_name", "rare_x_name", "name_x_department", "name_x_joint_work",
    "weak_orcid_only", "anonymous", "evidence_families", "token_coverage_min",
    "token_coverage_max", "token_match_score", "shared_long_tokens", "initial_expansions",
    "unmatched_tokens", "compatible_name", "rare_compatible_name", "name_x_joint_v2",
    "name_x_coauthors_v2",
)

# Each value is (training mean, training scale, fitted coefficient). Zero
# coefficients are omitted because they cannot affect inference.
_MODEL_TERMS = {
    "exact_name": (0.32063492063492066, 0.4667206533938249, -0.18268127644765073),
    "first_initial_equal": (0.8857142857142857, 0.318157963590287, 0.022003895826176133),
    "full_token_min": (1.3428571428571427, 0.5007024544036158, 0.6113346079178477),
    "same_orcid": (0.12380952380952381, 0.3293641231579159, 0.8253948318590189),
    "orcid_conflict": (0.05396825396825397, 0.22595504316538723, -1.1432529766111763),
    "one_orcid": (0.6095238095238096, 0.4878570847567885, 0.08682469383839454),
    "same_staff": (0.044444444444444446, 0.20608041101101562, 0.527869951352384),
    "staff_conflict": (0.009523809523809525, 0.09712418121129116, -0.11163134311126072),
    "trusted_catalog_count": (0.42857142857142855, 0.593998709695721, -0.053612890609981256),
    "display_only_count": (0.3746031746031746, 0.5849890119625377, -0.06565933153247627),
    "fallback_count": (0.19682539682539682, 0.3975992454594474, 1.1608568362186156),
    "shared_departments": (0.4031746031746032, 0.4905352612499987, 0.8367858914263532),
    "joint_works": (1.384126984126984, 5.488955905840015, 0.1435852838429137),
    "works_ratio": (0.3096627420871783, 0.3535447196616668, -0.5020902858679236),
    "surname_rarity": (0.30244974606504454, 0.07873277043060217, 0.8233763743290419),
    "name_x_coauthors": (0.5364064903251486, 0.9249678641977395, 0.5163211324051568),
    "initials_x_coauthors": (0.5838486076526043, 0.9433490681492799, 0.003765079207245306),
    "fallback_x_name": (0.1492063492063492, 0.35629175483423997, 0.07600494839781889),
    "rare_x_name": (0.18206006598242502, 0.15470255413509934, 0.3146533307063552),
    "name_x_department": (0.273015873015873, 0.44550892931259384, -0.3200200584860812),
    "weak_orcid_only": (0.03492063492063492, 0.18357882279112328, 0.24796801940487245),
    "anonymous": (0.01904761904761905, 0.13669238185149832, -0.5952693217950946),
    "token_coverage_min": (0.9005291005291004, 0.20993759579147367, 1.5046846924256279),
    "shared_long_tokens": (1.2222222222222223, 0.47956476446429697, -0.23083086550273704),
    "initial_expansions": (0.3682539682539683, 0.5141877233707601, 0.29788192318996226),
    "unmatched_tokens": (0.7841269841269841, 1.02866940548448, -0.18539374343201523),
    "compatible_name": (0.8698412698412699, 0.33647798608853596, -0.3129290416755121),
    "name_x_joint_v2": (0.2571428571428571, 0.4370588154508101, -0.5149459567658135),
    "name_x_coauthors_v2": (0.6305948260232472, 0.7631848975681033, -0.0217851278644652),
}
_MODEL_INTERCEPT = 1.333868946903036


def _tokens(value: str) -> list[str]:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(character for character in value if not unicodedata.combining(character))
    return re.findall(r"[a-z]+", value.casefold().translate(_CYRILLIC_TO_LATIN))


def _surname(tokens: list[str]) -> str:
    candidates = [token for token in tokens if len(token) > 1]
    return candidates[-1] if candidates else ""


def _token_alignment(first: list[str], second: list[str]) -> dict[str, float]:
    candidates = []
    for left_index, left in enumerate(first):
        for right_index, right in enumerate(second):
            if left == right:
                score = 1.0
            elif (len(left) == 1 and right.startswith(left)) or (len(right) == 1 and left.startswith(right)):
                score = 0.85
            else:
                similarity = SequenceMatcher(None, left, right).ratio()
                score = 0.70 if min(len(left), len(right)) >= 4 and similarity >= 0.86 else 0.0
            if score:
                candidates.append((score, left_index, right_index))

    used_first: set[int] = set()
    used_second: set[int] = set()
    matches = []
    for score, left_index, right_index in sorted(candidates, reverse=True):
        if left_index in used_first or right_index in used_second:
            continue
        used_first.add(left_index)
        used_second.add(right_index)
        matches.append((score, first[left_index], second[right_index]))

    count = len(matches)
    return {
        "token_coverage_min": count / max(1, min(len(first), len(second))),
        "token_coverage_max": count / max(1, max(len(first), len(second))),
        "token_match_score": sum(item[0] for item in matches) / max(1, max(len(first), len(second))),
        "shared_long_tokens": float(sum(len(left) > 1 and len(right) > 1 for _, left, right in matches)),
        "initial_expansions": float(sum((len(left) == 1) ^ (len(right) == 1) for _, left, right in matches)),
        "unmatched_tokens": float(len(first) + len(second) - 2 * count),
    }


def feature_vector(evidence: PairEvidence) -> dict[str, float]:
    """Build the exact 46-feature vector used by the fitted model."""
    first = _tokens(evidence.name_a)
    second = _tokens(evidence.name_b)
    normalized_first = " ".join(first)
    normalized_second = " ".join(second)
    first_set, second_set = set(first), set(second)
    surname_a, surname_b = _surname(first), _surname(second)
    full_a = [token for token in first if len(token) > 1]
    full_b = [token for token in second if len(token) > 1]
    same_orcid = int(bool(evidence.orcid_a) and evidence.orcid_a == evidence.orcid_b)
    orcid_conflict = int(bool(evidence.orcid_a) and bool(evidence.orcid_b) and evidence.orcid_a != evidence.orcid_b)
    same_staff = int(bool(evidence.staff_id_a) and evidence.staff_id_a == evidence.staff_id_b)
    staff_conflict = int(
        bool(evidence.staff_id_a) and bool(evidence.staff_id_b) and evidence.staff_id_a != evidence.staff_id_b
    )
    fallback_count = sum(identifier.startswith(("name_", "orcid_")) for identifier in (evidence.person_a, evidence.person_b))
    shared_coauthors = math.log1p(evidence.shared_coauthors)
    works_min = min(evidence.works_a, evidence.works_b)
    works_ratio = works_min / max(1, max(evidence.works_a, evidence.works_b))
    surname_rarity = 1 / math.log2(2 + evidence.surname_occurrences_a + evidence.surname_occurrences_b)

    values: dict[str, float] = {
        "name_similarity": SequenceMatcher(None, normalized_first, normalized_second).ratio(),
        "token_jaccard": len(first_set & second_set) / max(1, len(first_set | second_set)),
        "exact_name": float(normalized_first == normalized_second),
        "same_tokens": float(sorted(first) == sorted(second)),
        "surname_equal": float(bool(surname_a) and surname_a == surname_b),
        "first_initial_equal": float(bool(first) and bool(second) and first[0][0] == second[0][0]),
        "initial_count": float(sum(len(token) == 1 for token in first + second)),
        "full_token_min": float(min(len(full_a), len(full_b))),
        "same_orcid": float(same_orcid),
        "orcid_conflict": float(orcid_conflict),
        "one_orcid": float(bool(evidence.orcid_a) ^ bool(evidence.orcid_b)),
        "same_staff": float(same_staff),
        "staff_conflict": float(staff_conflict),
        "trusted_catalog_count": float(sum(
            status == "trusted_staff_identity" for status in (evidence.catalog_status_a, evidence.catalog_status_b)
        )),
        "display_only_count": float(sum(
            status == "display_match_only" for status in (evidence.catalog_status_a, evidence.catalog_status_b)
        )),
        "fallback_count": float(fallback_count),
        "shared_coauthors": shared_coauthors,
        "shared_departments": float(evidence.shared_departments),
        "shared_fields": float(evidence.shared_fields),
        "joint_works": float(evidence.shared_publications),
        "works_min": float(works_min),
        "works_ratio": works_ratio,
        "surname_rarity": surname_rarity,
    }
    values["name_corroboration"] = float(sum((
        evidence.shared_coauthors > 0,
        evidence.shared_departments > 0,
        evidence.shared_fields >= 2,
    )))
    values["strong_name"] = float(
        values["same_tokens"] == 1
        or (values["surname_equal"] == 1 and values["name_similarity"] >= 0.82)
    )
    values["initials_only_pair"] = float(values["initial_count"] >= 2 and values["full_token_min"] <= 1)
    values["orcid_x_name"] = values["same_orcid"] * values["strong_name"]
    values["name_x_coauthors"] = values["strong_name"] * values["shared_coauthors"]
    values["initials_x_coauthors"] = values["initials_only_pair"] * values["shared_coauthors"]
    values["fallback_x_name"] = min(values["fallback_count"], 1) * values["strong_name"]
    values["rare_x_name"] = values["surname_rarity"] * values["strong_name"]
    values["name_x_department"] = values["strong_name"] * min(values["shared_departments"], 1)
    values["name_x_joint_work"] = values["strong_name"] * min(values["joint_works"], 1)
    values["weak_orcid_only"] = float(
        values["same_orcid"] == 1
        and evidence.shared_coauthors == 0
        and evidence.shared_departments == 0
        and evidence.shared_fields <= 1
        and fallback_count == 0
    )
    values["anonymous"] = float("anonymous" in f"{evidence.name_a} {evidence.name_b}".casefold())
    values["evidence_families"] = float(sum((
        bool(values["strong_name"]),
        bool(same_orcid or same_staff),
        evidence.shared_coauthors > 0,
        evidence.shared_departments > 0,
        evidence.shared_fields >= 2,
        fallback_count > 0,
        evidence.shared_publications > 0,
    )))
    values.update(_token_alignment(first, second))
    values["compatible_name"] = float(values["token_coverage_min"] >= 0.66 and values["shared_long_tokens"] >= 1)
    values["rare_compatible_name"] = values["compatible_name"] * values["surname_rarity"]
    values["name_x_joint_v2"] = values["compatible_name"] * min(values["joint_works"], 1)
    values["name_x_coauthors_v2"] = values["compatible_name"] * min(values["shared_coauthors"], 2)
    return {name: float(values[name]) for name in MODEL_FEATURES}


def logistic_probability(evidence: PairEvidence) -> float:
    features = feature_vector(evidence)
    logit = _MODEL_INTERCEPT + sum(
        ((features[name] - mean) / scale) * coefficient
        for name, (mean, scale, coefficient) in _MODEL_TERMS.items()
    )
    if logit >= 0:
        return 1 / (1 + math.exp(-logit))
    exponent = math.exp(logit)
    return exponent / (1 + exponent)


def _hard_veto(evidence: PairEvidence) -> str | None:
    if evidence.orcid_a and evidence.orcid_b and evidence.orcid_a != evidence.orcid_b:
        return "conflicting ORCID"
    if evidence.staff_id_a and evidence.staff_id_b and evidence.staff_id_a != evidence.staff_id_b:
        return "conflicting staff identity"
    if evidence.profile_conflict:
        return "conflicting profile identifier"
    if evidence.initials_conflict:
        return "conflicting initials"
    if "anonymous" in f"{evidence.name_a} {evidence.name_b}".casefold():
        return "anonymous record"
    return None


def resolve_pair(evidence: PairEvidence, policy: ResolverPolicy = DEFAULT_POLICY) -> Resolution:
    """Apply vetoes, trusted identifiers and configurable confidence zones."""
    probability = logistic_probability(evidence)
    if reason := _hard_veto(evidence):
        return Resolution(Decision.SEPARATE, "hard_veto", probability, reason)
    if evidence.orcid_a and evidence.orcid_a == evidence.orcid_b:
        return Resolution(Decision.MERGE, "same_orcid", probability)
    if evidence.staff_id_a and evidence.staff_id_a == evidence.staff_id_b:
        return Resolution(Decision.MERGE, "same_staff", probability)
    if probability >= policy.merge_from:
        return Resolution(Decision.MERGE, "logreg_high", probability)
    if probability < policy.separate_below:
        return Resolution(Decision.SEPARATE, "logreg_low", probability)
    return Resolution(Decision.FIRST_MODEL, "qwen_first", probability)


def apply_first_verdict(resolution: Resolution, verdict: ModelVerdict) -> Resolution:
    """Send only positive first-stage verdicts to an independent judge."""
    if resolution.decision is not Decision.FIRST_MODEL:
        raise ValueError("first verdict requires a first-model resolution")
    if verdict.duplicate:
        return Resolution(Decision.SECOND_MODEL, "qwen_second", resolution.probability, verdict.reason)
    return Resolution(Decision.SEPARATE, "qwen_first_separate", resolution.probability, verdict.reason)


def apply_second_verdict(resolution: Resolution, verdict: ModelVerdict) -> Resolution:
    """Treat the independent second binary verdict as final."""
    if resolution.decision is not Decision.SECOND_MODEL:
        raise ValueError("second verdict requires a second-model resolution")
    decision = Decision.MERGE if verdict.duplicate else Decision.SEPARATE
    route = "qwen_second_merge" if verdict.duplicate else "qwen_second_separate"
    return Resolution(decision, route, resolution.probability, verdict.reason)
