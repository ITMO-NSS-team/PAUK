"""Read-only Cypher templates, one bundle per `graph_type`.

Each bundle deliberately pulls more than any one question needs - every
author, unit, repository and contributor around the entry points. Trimming
to what was asked happens afterwards in code: dropping extra rows is cheap
and deterministic, a row the query never fetched cannot be recovered.
"""

from __future__ import annotations

from typing import Any

from .reader import GraphReader

# --- topic_subgraph: everything around a set of publications/repositories ---

TOPIC_AUTHORSHIP = """
MATCH (pub:Publication) WHERE pub.id IN $pub_ids
MATCH (person:Person)-[a:AUTHORED]->(pub)
RETURN pub.id AS pub, person.id AS person, a.position AS position,
       COUNT { (:Person)-[:AUTHORED]->(pub) } AS n_authors
"""
TOPIC_PUBLICATIONS = """
MATCH (pub:Publication) WHERE pub.id IN $pub_ids
OPTIONAL MATCH (pub)-[:PRODUCED_BY]->(d:Department)
RETURN pub.id AS id, pub.title AS title, pub.year AS year, pub.journal AS journal, pub.doi AS doi,
       pub.openalex_url AS openalex_url, collect(DISTINCT d.id) AS departments
"""
TOPIC_REPOSITORIES = """
MATCH (r:Repository)
WHERE r.id IN $repo_ids OR EXISTS { (r)-[:IMPLEMENTS]->(p:Publication) WHERE p.id IN $pub_ids }
OPTIONAL MATCH (r)-[:IMPLEMENTS]->(implemented:Publication)
OPTIONAL MATCH (contributor:Person)-[c:CONTRIBUTED_TO]->(r)
RETURN r.id AS id, r.name AS name, r.url AS url, r.description AS description, r.stars_num AS stars,
       collect(DISTINCT implemented.id) AS publications,
       collect(DISTINCT {person: contributor.id, role: c.role}) AS contributors
"""

# --- shared lookups -----------------------------------------------------------

PERSONS = """
MATCH (p:Person) WHERE p.id IN $person_ids
OPTIONAL MATCH (p)-[:BELONGS_TO]->(d:Department)
RETURN p.id AS id, p.name_en AS name_en, p.name_ru AS name_ru, p.name_raw AS name_raw,
       p.name_variants AS name_variants, p.orcid AS orcid,
       p.openalex_id AS openalex_id, coalesce(p.is_itmo, false) AS is_itmo,
       collect(DISTINCT d.id) AS departments
"""
DEPARTMENTS = """
MATCH (d:Department) WHERE d.id IN $department_ids
OPTIONAL MATCH path = (d)-[:PART_OF*1..6]->(ancestor:Department)
WITH d, ancestor, min(length(path)) AS depth
RETURN d.id AS id, d.name_ru AS name_ru, d.name_en AS name_en, d.kind AS kind,
       collect(CASE WHEN ancestor IS NOT NULL THEN {id: ancestor.id, depth: depth} END) AS ancestors
"""

# --- person_profile -------------------------------------------------------------

PERSON_PUBLICATIONS = """
MATCH (p:Person {id: $person_id})-[:AUTHORED]->(pub:Publication)
OPTIONAL MATCH (pub)-[:PRODUCED_BY]->(d:Department)
RETURN pub.id AS id, pub.title AS title, pub.year AS year, pub.journal AS journal, pub.doi AS doi,
       pub.openalex_url AS openalex_url, collect(DISTINCT d.id) AS departments
"""
PERSON_REPOSITORIES = """
MATCH (p:Person {id: $person_id})
OPTIONAL MATCH (p)-[c:CONTRIBUTED_TO]->(contributed:Repository)
OPTIONAL MATCH (p)-[:AUTHORED]->(:Publication)<-[:IMPLEMENTS]-(implementing:Repository)
WITH collect(DISTINCT {id: contributed.id, role: c.role}) AS contributed,
     collect(DISTINCT implementing.id) AS implementing
UNWIND [x IN contributed WHERE x.id IS NOT NULL | x.id] + implementing AS rid
WITH DISTINCT rid
MATCH (r:Repository {id: rid})
OPTIONAL MATCH (r)-[:IMPLEMENTS]->(implemented:Publication)
OPTIONAL MATCH (contributor:Person)-[c2:CONTRIBUTED_TO]->(r)
RETURN r.id AS id, r.name AS name, r.url AS url, r.description AS description, r.stars_num AS stars,
       collect(DISTINCT implemented.id) AS publications,
       collect(DISTINCT {person: contributor.id, role: c2.role}) AS contributors
"""
# --- department_profile ---------------------------------------------------------

DEPARTMENT_UNITS = """
MATCH (d:Department {id: $department_id})
OPTIONAL MATCH (child:Department)-[:PART_OF*1..6]->(d)
WITH d, collect(DISTINCT child.id) AS children
RETURN [d.id] + children AS units
"""
DEPARTMENT_PUBLICATIONS = """
MATCH (pub:Publication)-[:PRODUCED_BY]->(d:Department) WHERE d.id IN $units
RETURN DISTINCT pub.id AS id
"""
DEPARTMENT_MEMBERS = """
MATCH (p:Person)-[:BELONGS_TO]->(d:Department) WHERE d.id IN $units
RETURN DISTINCT p.id AS id
"""


def topic_subgraph(graph: GraphReader, pub_ids: list[str], repo_ids: list[str]) -> dict[str, Any]:
    authorship = graph.read(TOPIC_AUTHORSHIP, pub_ids=pub_ids)
    publications = graph.read(TOPIC_PUBLICATIONS, pub_ids=pub_ids)
    repositories = graph.read(TOPIC_REPOSITORIES, pub_ids=pub_ids, repo_ids=repo_ids)
    return _with_lookups(graph, {
        "authorship": authorship,
        "publications": publications,
        "repositories": repositories,
    })


def person_profile(graph: GraphReader, person_id: str) -> dict[str, Any]:
    publications = graph.read(PERSON_PUBLICATIONS, person_id=person_id)
    # Every author of the person's papers, as in a topic subgraph: ranking
    # then scores co-authors by their shared papers through the same code,
    # and "who does X write with most" is answered by the persons section.
    authorship = graph.read(TOPIC_AUTHORSHIP, pub_ids=[row["id"] for row in publications])
    repositories = graph.read(PERSON_REPOSITORIES, person_id=person_id)
    return _with_lookups(graph, {
        "authorship": authorship,
        "publications": publications,
        "repositories": repositories,
    }, extra_persons=[person_id])


def department_units(graph: GraphReader, department_id: str) -> list[str]:
    rows = graph.read(DEPARTMENT_UNITS, department_id=department_id)
    return rows[0]["units"] if rows else [department_id]


def department_publication_ids(graph: GraphReader, units: list[str]) -> list[str]:
    return [row["id"] for row in graph.read(DEPARTMENT_PUBLICATIONS, units=units)]


def department_profile(graph: GraphReader, department_id: str) -> dict[str, Any]:
    units = department_units(graph, department_id)
    pub_ids = department_publication_ids(graph, units)
    members = [row["id"] for row in graph.read(DEPARTMENT_MEMBERS, units=units)]
    data = topic_subgraph(graph, pub_ids, [])
    data["units"] = units
    data["members"] = members
    if members:
        known = set(data["persons"])
        for row in graph.read(PERSONS, person_ids=[m for m in members if m not in known]):
            data["persons"][row["id"]] = row
    return data


def person_publication_ids(graph: GraphReader, person_id: str) -> list[str]:
    return [row["id"] for row in graph.read(PERSON_PUBLICATIONS, person_id=person_id)]


def _with_lookups(graph: GraphReader, data: dict[str, Any], extra_persons: list[str] | None = None) -> dict[str, Any]:
    person_ids = {row["person"] for row in data["authorship"]}
    for repo in data["repositories"]:
        person_ids.update(c["person"] for c in repo["contributors"] if c.get("person"))
    person_ids.update(extra_persons or [])
    persons = {row["id"]: row for row in graph.read(PERSONS, person_ids=sorted(person_ids))} if person_ids else {}
    department_ids = {d for row in data["publications"] for d in row.get("departments") or []}
    department_ids.update(d for row in persons.values() for d in row.get("departments") or [])
    departments = (
        {row["id"]: row for row in graph.read(DEPARTMENTS, department_ids=sorted(department_ids))}
        if department_ids else {}
    )
    # Ancestors are needed by name too, since scores roll up the hierarchy.
    missing = {a["id"] for row in departments.values() for a in row["ancestors"]} - departments.keys()
    if missing:
        departments.update({row["id"]: row for row in graph.read(DEPARTMENTS, department_ids=sorted(missing))})
    data["persons"] = persons
    data["departments"] = departments
    return data
