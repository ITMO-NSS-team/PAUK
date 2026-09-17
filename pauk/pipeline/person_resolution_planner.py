"""Pipeline adapter for confidence-based person resolution."""

from __future__ import annotations

from collections import Counter, defaultdict

from pauk.graph.person_resolution import (
    DEFAULT_POLICY,
    Decision,
    ModelVerdict,
    PairEvidence,
    ResearcherContext,
    ResolverPolicy,
    SecondStageContext,
    _identifier_relation,
    _surname,
    _tokens,
    apply_first_verdict,
    apply_second_verdict,
    resolve_pair,
)
from pauk.graph.person_resolution_model import LogisticModel
from pauk.models import Person

SAME = "same"
DIFFERENT = "different"


def plan_person_merges_resolved(
    people: list[Person],
    trusted_orcid: dict[str, str | None],
    *,
    in_scope: set[str] | None = None,
    fields_of: dict[str, set[str]] | None = None,
    staff_ids: dict[str, str] | None = None,
    decisions: dict[frozenset[str], str] | None = None,
    models=None,
    policy: ResolverPolicy = DEFAULT_POLICY,
    logreg_model: LogisticModel | None = None,
) -> tuple[list[tuple[Person, list[Person]]], list[dict]]:
    """Plan folds with LogReg and two independent model verdicts."""
    # Imported lazily to avoid a module cycle: DedupStage calls this adapter,
    # while these helpers remain the shared source of candidate generation and
    # transitive component safety checks.
    from pauk.pipeline.stages.dedup import (
        _group_conflict,
        _grouped,
        _initials_conflict,
        _is_pooled_record,
        _paired_persons,
        _profiles_conflict,
    )

    by_id = {person.id: person for person in people}
    fields_of = fields_of or {}
    staff_ids = staff_ids or {}
    decisions = decisions or {}
    publication_ids = {person.id: {authorship.publication_id for authorship in person.authored} for person in people}
    publication_authors: dict[str, set[str]] = defaultdict(set)
    for person in people:
        for publication_id in publication_ids[person.id]:
            publication_authors[publication_id].add(person.id)

    def all_coauthors(person_id: str, excluded_works: set[str] | None = None) -> set[str]:
        found: set[str] = set()
        for publication_id in publication_ids[person_id] - (excluded_works or set()):
            found |= publication_authors.get(publication_id, set())
        found.discard(person_id)
        return found

    def corroborating_coauthors(first_id: str, second_id: str) -> set[str]:
        together = publication_ids[first_id] & publication_ids[second_id]
        shared = all_coauthors(first_id, together) & all_coauthors(second_id, together)
        return shared - {first_id, second_id}

    def research_fields(person_id: str) -> set[str]:
        return {field for publication_id in publication_ids[person_id] for field in fields_of.get(publication_id, ())}

    surname_counts: Counter[str] = Counter()
    for person in people:
        surname = _surname(_tokens(person.name_raw or ""))
        if surname:
            surname_counts[surname] += 1

    pair_data: dict[int, tuple[Person, Person, PairEvidence, set[str], set[str]]] = {}
    merge_pairs: list[tuple[str, str]] = []
    pair_rules: dict[frozenset[str], str] = {}
    report: list[dict] = []
    pending_first: list[tuple[int, PairEvidence]] = []

    def plan_merge(first: Person, second: Person, route: str) -> None:
        pair = tuple(sorted((first.id, second.id)))
        if decisions.get(frozenset(pair)) == DIFFERENT:
            report.append(
                {
                    "status": "disputed",
                    "person_a": first.id,
                    "name_a": first.name_raw,
                    "person_b": second.id,
                    "name_b": second.name_raw,
                    "rule": route,
                }
            )
            return
        merge_pairs.append(pair)
        pair_rules[frozenset(pair)] = route

    def hold(
        first: Person,
        second: Person,
        evidence: PairEvidence,
        shared_fields: set[str],
        route: str,
        reason: str,
        confidence: float | None = None,
    ) -> None:
        if frozenset((first.id, second.id)) in decisions:
            return
        row = {
            "status": "held",
            "person_a": first.id,
            "name_a": first.name_raw,
            "person_b": second.id,
            "name_b": second.name_raw,
            "shared_coauthors": evidence.shared_coauthors,
            "shared_departments": evidence.shared_departments,
            "shared_publications": evidence.shared_publications,
            "shared_fields": sorted(shared_fields),
            "logreg_probability": round(resolve_pair(evidence, policy, logreg_model).probability, 6),
            "route": route,
            "held_because": [reason],
        }
        if confidence is not None:
            row["model_confidence"] = confidence
        report.append(row)

    candidates = list(_paired_persons(people, in_scope, staff_ids))
    seen = {frozenset((first.id, second.id)) for first, second in candidates}
    for members, verdict in decisions.items():
        if verdict != SAME or len(members) != 2:
            continue
        first_id, second_id = sorted(members)
        if first_id not in by_id or second_id not in by_id or members in seen:
            continue
        if in_scope is not None and first_id not in in_scope and second_id not in in_scope:
            continue
        candidates.append((by_id[first_id], by_id[second_id]))

    for pair_id, (first, second) in enumerate(candidates):
        if _is_pooled_record(first) or _is_pooled_record(second):
            continue
        answer = decisions.get(frozenset((first.id, second.id)))
        if answer == SAME:
            plan_merge(first, second, "manual")
            continue

        shared_coauthors = corroborating_coauthors(first.id, second.id)
        shared_departments = set(first.department_ids) & set(second.department_ids)
        shared_fields = research_fields(first.id) & research_fields(second.id)
        shared_publications = publication_ids[first.id] & publication_ids[second.id]
        surname_a = _surname(_tokens(first.name_raw or ""))
        surname_b = _surname(_tokens(second.name_raw or ""))
        evidence = PairEvidence(
            person_a=first.id,
            name_a=first.name_raw or "",
            person_b=second.id,
            name_b=second.name_raw or "",
            orcid_a=trusted_orcid.get(first.id, first.orcid),
            orcid_b=trusted_orcid.get(second.id, second.orcid),
            staff_id_a=staff_ids.get(first.id),
            staff_id_b=staff_ids.get(second.id),
            catalog_status_a="trusted_staff_identity" if first.id in staff_ids else "",
            catalog_status_b="trusted_staff_identity" if second.id in staff_ids else "",
            shared_coauthors=len(shared_coauthors),
            shared_departments=len(shared_departments),
            shared_fields=len(shared_fields),
            shared_publications=len(shared_publications),
            works_a=len(publication_ids[first.id]),
            works_b=len(publication_ids[second.id]),
            surname_occurrences_a=surname_counts.get(surname_a, 0),
            surname_occurrences_b=surname_counts.get(surname_b, 0),
            profile_conflict=_profiles_conflict(first, second),
            initials_conflict=_initials_conflict(first, second),
        )
        pair_data[pair_id] = (first, second, evidence, shared_coauthors, shared_fields)
        resolution = resolve_pair(evidence, policy, logreg_model)
        if resolution.decision is Decision.MERGE:
            plan_merge(first, second, resolution.route)
        elif resolution.decision is Decision.FIRST_MODEL:
            pending_first.append((pair_id, evidence))

    first_results = models.first_many(pending_first) if models is not None else {}
    pending_second: list[SecondStageContext] = []
    first_positive: dict[int, ModelVerdict] = {}

    def researcher_context(person: Person) -> ResearcherContext:
        field_counts = Counter(
            field for publication_id in publication_ids[person.id] for field in fields_of.get(publication_id, ())
        )
        coauthor_counts = Counter()
        for publication_id in publication_ids[person.id]:
            for coauthor in publication_authors.get(publication_id, ()) - {person.id}:
                coauthor_counts[coauthor] += 1
        return ResearcherContext(
            person_id=person.id,
            name=person.name_raw or "",
            aliases=tuple(person.name_variants),
            fallback_record=person.id.startswith(("name_", "orcid_")),
            works=len(publication_ids[person.id]),
            top_fields=tuple(name for name, _ in field_counts.most_common(8)),
            departments=tuple({"id": value} for value in person.department_ids[:8]),
            top_coauthors=tuple(
                {
                    "id": coauthor_id,
                    "name": by_id[coauthor_id].name_raw if coauthor_id in by_id else "",
                    "shared_works": count,
                }
                for coauthor_id, count in coauthor_counts.most_common(12)
            ),
        )

    for pair_id, _evidence in pending_first:
        first, second, evidence, shared_coauthors, shared_fields = pair_data[pair_id]
        verdict = first_results.get(pair_id)
        if verdict is None:
            hold(first, second, evidence, shared_fields, "qwen_first", "first model unavailable")
            continue
        resolution = apply_first_verdict(resolve_pair(evidence, policy, logreg_model), verdict)
        if resolution.decision is Decision.SEPARATE:
            hold(
                first,
                second,
                evidence,
                shared_fields,
                resolution.route,
                verdict.reason or "first model rejected the merge",
                verdict.confidence,
            )
            continue
        first_positive[pair_id] = verdict
        coauthors_a = all_coauthors(first.id)
        coauthors_b = all_coauthors(second.id)
        pending_second.append(
            SecondStageContext(
                pair_id=pair_id,
                researcher_a=researcher_context(first),
                researcher_b=researcher_context(second),
                trusted_orcid_relation=_identifier_relation(evidence.orcid_a, evidence.orcid_b),
                staff_identity_relation=_identifier_relation(evidence.staff_id_a, evidence.staff_id_b),
                shared_work_ids=tuple(sorted(publication_ids[first.id] & publication_ids[second.id])[:12]),
                shared_coauthors=tuple(
                    {
                        "id": coauthor_id,
                        "name": by_id[coauthor_id].name_raw if coauthor_id in by_id else "",
                    }
                    for coauthor_id in sorted(shared_coauthors)[:12]
                ),
                shared_fields=tuple(sorted(shared_fields)[:12]),
                logreg_probability=resolve_pair(evidence, policy, logreg_model).probability,
                first_verdict=verdict,
                impact_not_identity_evidence={
                    "distinct_neighbors_a": len(coauthors_a - coauthors_b),
                    "distinct_neighbors_b": len(coauthors_b - coauthors_a),
                    "shared_neighbors": len(coauthors_a & coauthors_b),
                    "potential_cross_neighbor_pairs": len(coauthors_a - coauthors_b) * len(coauthors_b - coauthors_a),
                },
            )
        )

    second_results = models.second_many(pending_second) if models is not None else {}
    for context in pending_second:
        pair_id = context.pair_id
        first, second, evidence, _shared_coauthors, shared_fields = pair_data[pair_id]
        verdict = second_results.get(pair_id)
        if verdict is None:
            hold(first, second, evidence, shared_fields, "qwen_second", "second model unavailable")
            continue
        resolution = apply_second_verdict(
            apply_first_verdict(resolve_pair(evidence, policy, logreg_model), first_positive[pair_id]),
            verdict,
        )
        if resolution.decision is Decision.MERGE:
            plan_merge(first, second, resolution.route)
        else:
            hold(
                first,
                second,
                evidence,
                shared_fields,
                resolution.route,
                verdict.reason or "independent model rejected the merge",
                verdict.confidence,
            )

    groups: list[tuple[Person, list[Person]]] = []
    for members in _grouped(merge_pairs):
        conflict = _group_conflict(members, by_id, trusted_orcid, staff_ids)
        if conflict:
            field, values = conflict
            report.append(
                {
                    "status": "held",
                    "persons": sorted(members),
                    "names": [by_id[member].name_raw for member in sorted(members)],
                    "held_because": [f"group spans {len(values)} distinct {field} values"],
                    "route": "component_conflict",
                }
            )
            continue
        ranked = sorted(
            (by_id[member] for member in members),
            key=lambda person: (
                -len(person.authored),
                person.orcid is None,
                person.id,
            ),
        )
        canonical, duplicates = ranked[0], ranked[1:]
        groups.append((canonical, duplicates))
        for duplicate in duplicates:
            report.append(
                {
                    "status": "merged",
                    "person_a": duplicate.id,
                    "name_a": duplicate.name_raw,
                    "person_b": canonical.id,
                    "name_b": canonical.name_raw,
                    "merged_into": canonical.id,
                    "rules": sorted({rule for pair, rule in pair_rules.items() if duplicate.id in pair}),
                }
            )
    return groups, report
