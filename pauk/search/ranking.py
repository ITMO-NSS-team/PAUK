"""Turning the fetched subgraph into ranked, trimmed sections.

Relevance enters only through publications and repositories (the nodes
with text); people and units inherit it along the graph's edges. Every
weight can be switched off so the evaluation can measure what each one adds.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date
from typing import Any

from .plan import QueryPlan


@dataclass(frozen=True)
class RankingConfig:
    use_position: bool = True
    use_recency: bool = True
    use_repositories: bool = True
    use_hierarchy: bool = True
    current_year: int = date.today().year
    recency_half_life: float = 4.0
    middle_author_weight: float = 0.5
    repository_weight: float = 0.5
    # Each level up the unit hierarchy keeps this share of a publication, so a
    # megafaculty does not outrank its own lab just by summing all of its labs.
    hierarchy_decay: float = 0.3
    # Relevance is raised to this power before it is summed, so a strong match
    # outweighs several weak ones: with 1, three loosely related papers on ion
    # diffusion put a chemist above the author of one diffusion-model paper.
    relevance_power: float = 2.0
    top_n: int = 10


# "authorship" is the classic document-based expert model without the graph's
# extra structure; "graph" adds author position, recency, code and hierarchy.
PRESETS: dict[str, RankingConfig] = {
    "authorship": RankingConfig(use_position=False, use_recency=False, use_repositories=False, use_hierarchy=False),
    "graph": RankingConfig(),
}


def preset(name: str, **overrides: Any) -> RankingConfig:
    return replace(PRESETS[name], **overrides)


def position_weight(position: int | None, n_authors: int | None, config: RankingConfig) -> float:
    if not config.use_position or not position or not n_authors or n_authors <= 2:
        return 1.0
    # First authors usually did the work, last authors usually lead the group.
    return 1.0 if position in (1, n_authors) else config.middle_author_weight


def recency_weight(year: int | None, config: RankingConfig) -> float:
    if not config.use_recency or not year:
        return 1.0
    age = max(0, config.current_year - year)
    return 0.5 ** (age / config.recency_half_life)


def publication_url(row: dict[str, Any]) -> str | None:
    doi = row.get("doi")
    if doi:
        return doi if doi.startswith("http") else f"https://doi.org/{doi}"
    return row.get("openalex_url")


def person_url(row: dict[str, Any]) -> str | None:
    if row.get("orcid"):
        return f"https://orcid.org/{row['orcid']}"
    if row.get("openalex_id"):
        return f"https://openalex.org/{row['openalex_id']}"
    return None


def person_label(row: dict[str, Any]) -> str:
    # Authors that author_names has not reached yet carry only the raw
    # OpenAlex spelling.
    variants = row.get("name_variants") or []
    return row.get("name_ru") or row.get("name_en") or row.get("name_raw") or (variants[0] if variants else row["id"])


def _year_allowed(year: int | None, plan: QueryPlan) -> bool:
    year_from, year_to = plan.filter.year_from, plan.filter.year_to
    if year_from is None and year_to is None:
        return True
    if year is None:
        return False
    return (year_from is None or year >= year_from) and (year_to is None or year <= year_to)


def rank(data: dict[str, Any], pub_relevance: dict[str, float], repo_relevance: dict[str, float],
         plan: QueryPlan, config: RankingConfig) -> dict[str, list[dict[str, Any]]]:
    publications = {row["id"]: row for row in data["publications"]}
    persons = data["persons"]
    departments = data["departments"]
    relevance = {
        pub_id: score ** config.relevance_power for pub_id, score in pub_relevance.items()
        if pub_id in publications and _year_allowed(publications[pub_id].get("year"), plan)
    }
    repo_relevance = {repo_id: score ** config.relevance_power for repo_id, score in repo_relevance.items()}

    # --- publications
    pub_items = []
    authors_of: dict[str, list[str]] = defaultdict(list)
    for row in sorted(data["authorship"], key=lambda r: r.get("position") or 0):
        authors_of[row["pub"]].append(row["person"])
    for pub_id, score in relevance.items():
        row = publications[pub_id]
        names = [person_label(persons[p]) for p in authors_of[pub_id] if p in persons]
        pub_items.append({
            "id": pub_id, "label": row.get("title") or pub_id, "url": publication_url(row),
            "title": row.get("title"), "year": row.get("year"), "journal": row.get("journal"),
            "authors": names[:6] + (["…"] if len(names) > 6 else []), "score": score,
        })

    # --- repositories
    repo_items = []
    repo_scores: dict[str, float] = {}
    for repo in data["repositories"]:
        linked = [relevance[p] for p in repo.get("publications") or [] if p in relevance]
        score = max([repo_relevance.get(repo["id"], 0.0), *linked])
        if score <= 0:
            continue
        repo_scores[repo["id"]] = score
        contributors = [c["person"] for c in repo["contributors"] if c.get("person") in persons]
        repo_items.append({
            "id": repo["id"], "label": repo.get("name") or repo["id"], "url": repo.get("url"),
            "name": repo.get("name"), "description": repo.get("description"), "stars": repo.get("stars"),
            "contributors": [person_label(persons[p]) for p in contributors], "score": score,
        })

    # --- persons
    person_score: dict[str, float] = defaultdict(float)
    person_pubs: dict[str, list[str]] = defaultdict(list)
    for row in data["authorship"]:
        pub_id = row["pub"]
        if pub_id not in relevance:
            continue
        year = publications[pub_id].get("year")
        weight = position_weight(row.get("position"), row.get("n_authors"), config) * recency_weight(year, config)
        person_score[row["person"]] += relevance[pub_id] * weight
        person_pubs[row["person"]].append(pub_id)
    person_repos: dict[str, list[str]] = defaultdict(list)
    for repo in data["repositories"]:
        if repo["id"] not in repo_scores:
            continue
        for contributor in repo["contributors"]:
            person = contributor.get("person")
            if not person:
                continue
            person_repos[person].append(repo["id"])
            if config.use_repositories:
                person_score[person] += config.repository_weight * repo_scores[repo["id"]]
    person_items = []
    repo_url = {repo["id"]: repo.get("url") for repo in data["repositories"]}
    for person_id, score in person_score.items():
        row = persons.get(person_id)
        if row is None or (plan.filter.only_itmo and not row.get("is_itmo")):
            continue
        top_pubs = sorted(person_pubs[person_id], key=lambda p: -relevance[p])[:3]
        person_items.append({
            "id": person_id, "label": person_label(row), "url": person_url(row),
            "name": person_label(row), "name_ru": row.get("name_ru"), "name_en": row.get("name_en"),
            "orcid": row.get("orcid"), "is_itmo": row.get("is_itmo"),
            "departments": [departments[d]["name_ru"] for d in row.get("departments") or [] if d in departments],
            "publications": len(set(person_pubs[person_id])),
            "top_publications": [publications[p].get("title") for p in top_pubs],
            "repositories": [repo_url[r] for r in dict.fromkeys(person_repos[person_id]) if repo_url.get(r)],
            "score": score,
        })

    # --- departments: a publication counts once per unit, wherever below that
    # unit it was produced, discounted by how many levels up the unit sits.
    def unit_weights(direct: list[str]) -> dict[str, float]:
        # A publication tagged with both a lab and its faculty was made in the
        # lab; the faculty is only its ancestor and gets the decayed share.
        above = {a["id"] for unit in direct for a in departments.get(unit, {}).get("ancestors", [])}
        direct = [unit for unit in direct if unit not in above]
        weights = {unit: 1.0 for unit in direct}
        if config.use_hierarchy:
            for unit in direct:
                for ancestor in departments.get(unit, {}).get("ancestors", []):
                    weight = config.hierarchy_decay ** ancestor["depth"]
                    weights[ancestor["id"]] = max(weights.get(ancestor["id"], 0.0), weight)
        return weights

    unit_score: dict[str, float] = defaultdict(float)
    unit_pubs: dict[str, set[str]] = defaultdict(set)
    for pub_id, score in relevance.items():
        recency = recency_weight(publications[pub_id].get("year"), config)
        for unit, weight in unit_weights(publications[pub_id].get("departments") or []).items():
            unit_score[unit] += score * recency * weight
            unit_pubs[unit].add(pub_id)
    unit_people: dict[str, set[str]] = defaultdict(set)
    for item in person_items:
        for unit in unit_weights(persons[item["id"]].get("departments") or []):
            unit_people[unit].add(item["id"])
    department_items = [
        {
            "id": unit, "label": departments[unit]["name_ru"] or departments[unit]["name_en"] or unit, "url": None,
            "name": departments[unit]["name_ru"], "kind": departments[unit].get("kind"),
            "publications": len(unit_pubs[unit]), "persons": len(unit_people[unit]), "score": score,
        }
        for unit, score in unit_score.items() if unit in departments
    ]

    sections = {
        "persons": person_items,
        "departments": department_items,
        "publications": pub_items,
        "repositories": repo_items,
    }
    for items in sections.values():
        # In a profile every publication weighs the same; show the newest first.
        items.sort(key=lambda item: (-item["score"], -(item.get("year") or 0)))
    return sections


ALWAYS_KEPT = ("id", "label", "url")


def trim(sections: dict[str, list[dict[str, Any]]], plan: QueryPlan, top_n: int) -> dict[str, list[dict[str, Any]]]:
    """Keep only the sections, fields and number of rows the question asked for."""
    trimmed = {}
    for name, items in sections.items():
        if name not in plan.expected_values:
            continue
        wanted = plan.wanted_fields(name)
        rows = items[:top_n]
        if wanted:
            keep = set(ALWAYS_KEPT) | wanted
            rows = [{key: value for key, value in row.items() if key in keep} for row in rows]
        trimmed[name] = rows
    return trimmed
