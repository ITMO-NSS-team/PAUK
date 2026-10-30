"""Pipeline adapter for confidence-based person resolution."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, replace

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
    is_multi_person_name,
    resolve_pair,
    shared_identity_facts,
)
from pauk.graph.person_resolution_model import LogisticModel
from pauk.graph.person_resolution_single import SingleVerdict, apply_verdict
from pauk.models import Person
from pauk.pipeline.person_name_blocking import NAME_RESCUE_TEAM_LIMIT
from pauk.pipeline.person_name_facts import name_comparison_facts

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
    """Plan folds with LogReg, full-context adjudication and component checks."""
    # Imported lazily to avoid a module cycle: DedupStage calls this adapter,
    # and these helpers are the shared candidate generation and component safety checks.
    from pauk.pipeline.stages.dedup import (
        _finalize_person_merge_groups,
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

    def all_coauthors(
        person_id: str, excluded_works: set[str] | None = None, *, small_team_only: bool = False
    ) -> set[str]:
        found: set[str] = set()
        for publication_id in publication_ids[person_id] - (excluded_works or set()):
            authors = publication_authors.get(publication_id, set())
            if not small_team_only or len(authors) <= NAME_RESCUE_TEAM_LIMIT:
                found |= authors
        found.discard(person_id)
        return found

    def corroborating_coauthors(first_id: str, second_id: str, *, small_team_only: bool = False) -> set[str]:
        together = publication_ids[first_id] & publication_ids[second_id]
        shared = all_coauthors(first_id, together, small_team_only=small_team_only) & all_coauthors(
            second_id, together, small_team_only=small_team_only
        )
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
    input_routes: dict[int, str] = {}

    def separate(first: Person, second: Person, resolution, **details) -> None:
        report.append(
            {
                "status": "separate",
                "person_a": first.id,
                "name_a": first.name_raw,
                "person_b": second.id,
                "name_b": second.name_raw,
                "route": resolution.route,
                "reason": resolution.reason,
                "logreg_probability": round(resolution.probability, 6),
                **details,
            }
        )

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
        *,
        input_route: str | None = None,
        first_verdict: ModelVerdict | None = None,
        second_verdict: ModelVerdict | None = None,
        single_verdict: SingleVerdict | None = None,
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
            "small_team_shared_coauthors": evidence.small_team_shared_coauthors,
            "small_team_shared_publications": evidence.small_team_shared_publications,
            "shared_departments": evidence.shared_departments,
            "shared_publications": evidence.shared_publications,
            "shared_fields": sorted(shared_fields),
            "logreg_probability": round(resolve_pair(evidence, policy, logreg_model).probability, 6),
            "route": route,
            "reason": reason,
            "held_because": [reason],
        }
        if confidence is not None:
            row["model_confidence"] = confidence
        if input_route is not None:
            row["input_route"] = input_route
            row["model_verdicts"] = {
                "first": asdict(first_verdict) if first_verdict is not None else None,
                "second": asdict(second_verdict) if second_verdict is not None else None,
            }
            if single_mode:
                row["model_verdicts"] = {"single": asdict(single_verdict) if single_verdict is not None else None}
        report.append(row)

    single_mode = models is not None and callable(getattr(type(models), "single_many", None))
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
        malformed = is_multi_person_name(first.name_raw or "") or is_multi_person_name(second.name_raw or "")
        if answer == SAME and not malformed:
            plan_merge(first, second, "manual")
            continue

        shared_coauthors = corroborating_coauthors(first.id, second.id)
        small_team_coauthors = corroborating_coauthors(first.id, second.id, small_team_only=True)
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
            small_team_shared_coauthors=len(small_team_coauthors),
            small_team_shared_publications=sum(
                len(publication_authors[work]) <= NAME_RESCUE_TEAM_LIMIT for work in shared_publications
            ),
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
        elif resolution.decision is Decision.SEPARATE:
            separate(first, second, resolution)
        elif resolution.decision is Decision.FIRST_MODEL:
            input_routes[pair_id] = resolution.route
            evidence = replace(evidence, name_facts=name_comparison_facts(first, second))
            pair_data[pair_id] = (first, second, evidence, shared_coauthors, shared_fields)
            pending_first.append((pair_id, evidence))
        elif resolution.decision is Decision.REVIEW:
            if answer == SAME:
                report.append(
                    {
                        "status": "disputed",
                        "person_a": first.id,
                        "name_a": first.name_raw,
                        "person_b": second.id,
                        "name_b": second.name_raw,
                        "rule": resolution.route,
                    }
                )
            else:
                hold(first, second, evidence, shared_fields, resolution.route, resolution.reason)

    first_results = models.first_many(pending_first) if models is not None and not single_mode else {}
    pending_second: list[SecondStageContext] = []
    first_verdicts: dict[int, ModelVerdict] = {}

    context_cache: dict[str, ResearcherContext] = {}

    def researcher_context(person: Person) -> ResearcherContext:
        if person.id in context_cache:
            return context_cache[person.id]
        field_counts = Counter(
            field for publication_id in publication_ids[person.id] for field in fields_of.get(publication_id, ())
        )
        coauthor_counts = Counter()
        small_team_counts = Counter()
        for publication_id in publication_ids[person.id]:
            for coauthor in publication_authors.get(publication_id, ()) - {person.id}:
                coauthor_counts[coauthor] += 1
                if len(publication_authors[publication_id]) <= NAME_RESCUE_TEAM_LIMIT:
                    small_team_counts[coauthor] += 1
        context = ResearcherContext(
            person_id=person.id,
            name=person.name_raw or "",
            aliases=tuple(person.name_variants),
            fallback_record=person.id.startswith(("name_", "orcid_")),
            works=len(publication_ids[person.id]),
            top_fields=tuple(
                name for name, _ in sorted(field_counts.items(), key=lambda item: (-item[1], item[0]))[:8]
            ),
            departments=tuple({"id": value} for value in person.department_ids[:8]),
            top_coauthors=tuple(
                {
                    "id": coauthor_id,
                    "name": by_id[coauthor_id].name_raw if coauthor_id in by_id else "",
                    "shared_works": count,
                    "small_team_shared_works": small_team_counts[coauthor_id],
                }
                for coauthor_id, count in sorted(
                    coauthor_counts.items(), key=lambda item: (-small_team_counts[item[0]], -item[1], item[0])
                )[:12]
            ),
        )
        context_cache[person.id] = context
        return context

    for pair_id, _evidence in pending_first:
        first, second, evidence, shared_coauthors, shared_fields = pair_data[pair_id]
        verdict = first_results.get(pair_id)
        if verdict is None and not single_mode:
            hold(
                first,
                second,
                evidence,
                shared_fields,
                "qwen_first",
                "first model unavailable",
                input_route=input_routes[pair_id],
            )
            continue
        if verdict is not None:
            first_verdicts[pair_id] = verdict
        coauthors_a = all_coauthors(first.id)
        coauthors_b = all_coauthors(second.id)
        small_team_coauthors = corroborating_coauthors(first.id, second.id, small_team_only=True)
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
                        "name": (by_id[coauthor_id].name_raw if coauthor_id in by_id else ""),
                        "small_team_supported": coauthor_id in small_team_coauthors,
                    }
                    for coauthor_id in sorted(
                        shared_coauthors, key=lambda identifier: (identifier not in small_team_coauthors, identifier)
                    )[:12]
                ),
                shared_fields=tuple(sorted(shared_fields)[:12]),
                logreg_probability=resolve_pair(evidence, policy, logreg_model).probability,
                first_verdict=verdict,
                identity_facts=shared_identity_facts(evidence),
                collaboration_evidence={
                    "small_team_size_limit": NAME_RESCUE_TEAM_LIMIT,
                    "shared_coauthors": evidence.shared_coauthors,
                    "small_team_shared_coauthors": evidence.small_team_shared_coauthors,
                    "shared_publications": evidence.shared_publications,
                    "small_team_shared_publications": evidence.small_team_shared_publications,
                },
                impact_not_identity_evidence={
                    "distinct_neighbors_a": len(coauthors_a - coauthors_b),
                    "distinct_neighbors_b": len(coauthors_b - coauthors_a),
                    "shared_neighbors": len(coauthors_a & coauthors_b),
                    "potential_cross_neighbor_pairs": len(coauthors_a - coauthors_b) * len(coauthors_b - coauthors_a),
                },
            )
        )

    if single_mode:
        second_results = models.single_many([(context, pair_data[context.pair_id][2]) for context in pending_second])
    else:
        second_results = models.second_many(pending_second) if models is not None else {}
    for context in pending_second:
        pair_id = context.pair_id
        first, second, evidence, _shared_coauthors, shared_fields = pair_data[pair_id]
        verdict = second_results.get(pair_id)
        if verdict is None:
            hold(
                first,
                second,
                evidence,
                shared_fields,
                "qwen_single" if single_mode else "qwen_second",
                "model unavailable" if single_mode else "second model unavailable",
                input_route=input_routes[pair_id],
                first_verdict=first_verdicts.get(pair_id),
            )
            continue
        if single_mode:
            resolution = apply_verdict(resolve_pair(evidence, policy, logreg_model), evidence, verdict)
        else:
            resolution = apply_second_verdict(
                apply_first_verdict(resolve_pair(evidence, policy, logreg_model), first_verdicts[pair_id]),
                verdict,
            )
        if resolution.decision is Decision.MERGE:
            plan_merge(first, second, resolution.route)
        elif resolution.decision is Decision.SEPARATE:
            separate(
                first,
                second,
                resolution,
                input_route=input_routes[pair_id],
                model_verdicts={"single": asdict(verdict)}
                if single_mode
                else {"first": asdict(first_verdicts[pair_id]), "second": asdict(verdict)},
            )
        elif resolution.decision is Decision.REVIEW:
            hold(
                first,
                second,
                evidence,
                shared_fields,
                resolution.route,
                resolution.reason,
                verdict.confidence,
                input_route=input_routes[pair_id],
                first_verdict=first_verdicts.get(pair_id),
                second_verdict=None if single_mode else verdict,
                single_verdict=verdict if single_mode else None,
            )

    groups, group_report = _finalize_person_merge_groups(
        merge_pairs,
        pair_rules,
        by_id,
        trusted_orcid,
        staff_ids,
        decisions,
    )
    report.extend(group_report)
    return groups, report
