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
_YEAR_FILTER = (
    "pub.year >= coalesce($year_from, pub.year) "
    "AND pub.year <= coalesce($year_to, pub.year)"
)



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

DEPARTMENT_PUBLICATIONS = """
MATCH (pub:Publication)-[:PRODUCED_BY]->(d:Department) WHERE d.id IN $units
RETURN DISTINCT pub.id AS id
"""
DEPARTMENT_MEMBERS = """
MATCH (p:Person)-[:BELONGS_TO]->(d:Department) WHERE d.id IN $units
RETURN DISTINCT p.id AS id
"""

T1_BY_DEPT = """
MATCH (p:Person)-[:BELONGS_TO]->(d:Department {id: $dept_id})
WHERE coalesce(p.is_itmo, false) = true
OPTIONAL MATCH (p)-[:AUTHORED]->(pub:Publication)
  WHERE pub.year >= coalesce($year_from, pub.year)
    AND pub.year <= coalesce($year_to, pub.year)
WITH p, collect(DISTINCT pub.id) AS pub_ids
RETURN p.id AS person_id, p.name_en AS name_en, p.name_ru AS name_ru,
       p.orcid AS orcid, p.degree AS degree,
       p.github AS github, p.google_scholar AS google_scholar,
       p.linkedin AS linkedin,
       p.is_itmo AS is_itmo,
       p.other_names AS other_names,
       size(pub_ids) AS publication_count
ORDER BY p.name_en
"""

T1_ITMO = """
MATCH (p:Person)
WHERE coalesce(p.is_itmo, false) = true
OPTIONAL MATCH (p)-[:AUTHORED]->(pub:Publication)
  WHERE pub.year >= coalesce($year_from, pub.year)
    AND pub.year <= coalesce($year_to, pub.year)
WITH p, collect(DISTINCT pub.id) AS pub_ids
RETURN p.id AS person_id, p.name_en AS name_en, p.name_ru AS name_ru,
       p.orcid AS orcid, p.degree AS degree,
       p.github AS github, p.google_scholar AS google_scholar,
       p.linkedin AS linkedin,
       p.is_itmo AS is_itmo,
       p.other_names AS other_names,
       size(pub_ids) AS publication_count
ORDER BY p.name_en
"""

T2_BY_DEPT = """
MATCH (p:Person)-[:BELONGS_TO]->(d:Department {id: $dept_id})
WHERE coalesce(p.is_itmo, false) = true
MATCH (p)-[:AUTHORED]->(pub:Publication)
WHERE pub.year >= coalesce($year_from, pub.year)
  AND pub.year <= coalesce($year_to, pub.year)
OPTIONAL MATCH (pub)-[:PRODUCED_BY]->(dept:Department)
RETURN DISTINCT pub.id AS publication_id, pub.title AS title, pub.year AS year,
       pub.journal AS journal, pub.type AS type, pub.doi AS doi,
       pub.funding AS funding, pub.pdf_urls AS pdf_urls,
       collect(DISTINCT dept.id) AS departments
"""

T2_ITMO = """
MATCH (p:Person)
WHERE coalesce(p.is_itmo, false) = true
MATCH (p)-[:AUTHORED]->(pub:Publication)
WHERE pub.year >= coalesce($year_from, pub.year)
  AND pub.year <= coalesce($year_to, pub.year)
OPTIONAL MATCH (pub)-[:PRODUCED_BY]->(dept:Department)
RETURN DISTINCT pub.id AS publication_id, pub.title AS title, pub.year AS year,
       pub.journal AS journal, pub.type AS type, pub.doi AS doi,
       pub.funding AS funding, pub.pdf_urls AS pdf_urls,
       collect(DISTINCT dept.id) AS departments
"""

T3A = """
MATCH (p:Person) WHERE p.id IN $person_ids
MATCH (p)-[:AUTHORED]->(pub:Publication)<-[:AUTHORED]-(coauthor:Person)
WHERE NOT coauthor.id IN $person_ids
WITH coauthor, 
     collect(DISTINCT p.id) AS shared_with, 
     collect(DISTINCT pub.id) AS shared_pub_ids
WHERE size(shared_with) = size($person_ids)
RETURN coauthor.id AS person_id, 
       coauthor.name_en AS name_en, 
       coauthor.name_ru AS name_ru,
       size(shared_with) AS shared_with_count,
       shared_pub_ids
ORDER BY shared_with_count DESC
"""

T3B = """
MATCH (p:Person {id: $person_id})-[:AUTHORED]->(pub:Publication)
WHERE pub.year >= coalesce($year_from, pub.year)
  AND pub.year <= coalesce($year_to, pub.year)
MATCH (coauthor:Person)-[:AUTHORED]->(pub)
WHERE coauthor.id <> $person_id
  AND coalesce(coauthor.is_itmo, false) = true
WITH coauthor, collect(DISTINCT pub.id) AS shared_pubs
RETURN coauthor.id AS person_id, coauthor.name_en AS name_en, coauthor.name_ru AS name_ru,
       size(shared_pubs) AS joint_publications,
       shared_pubs AS shared_pub_ids
ORDER BY joint_publications DESC
"""
# MATCH (p:Person)-[:AUTHORED]->(pub:Publication)
# WHERE ($person_id IS NULL OR p.id = $person_id)
#   AND pub.year >= coalesce($year_from, pub.year)
#   AND pub.year <= coalesce($year_to, pub.year)
# MATCH (coauthor:Person)-[:AUTHORED]->(pub)
# WHERE coauthor <> p
#   AND coalesce(coauthor.is_itmo, false) = true
# WITH coauthor, collect(DISTINCT pub.id) AS shared_pubs
# RETURN coauthor.id AS person_id,
#        coauthor.name_en AS name_en,
#        coauthor.name_ru AS name_ru,
#        size(shared_pubs) AS joint_publications,
#        shared_pubs AS shared_pub_ids
# ORDER BY joint_publications DESC

T3C = """
MATCH (p1:Person)-[:BELONGS_TO]->(d1:Department {id: $dept_id})
MATCH (p2:Person)-[:BELONGS_TO]->(d2:Department {id: $partner_dept_id})
MATCH (p1)-[:AUTHORED]->(pub:Publication)<-[:AUTHORED]-(p2)
WHERE pub.year >= coalesce($year_from, pub.year)
  AND pub.year <= coalesce($year_to, pub.year)
RETURN DISTINCT pub.id AS publication_id, pub.title AS title, pub.year AS year,
       pub.journal AS journal, pub.doi AS doi,
       p1.id AS person_from_subject, p2.id AS person_from_partner
ORDER BY pub.year DESC
"""

T3D = """
MATCH (p:Person {id: $person_id})-[:AUTHORED]->(pub:Publication)
WHERE pub.year >= coalesce($year_from, pub.year)
  AND pub.year <= coalesce($year_to, pub.year)
MATCH (coauthor:Person)-[:BELONGS_TO]->(:Department {id: $partner_dept_id})
MATCH (coauthor)-[:AUTHORED]->(pub)
WHERE coauthor.id <> $person_id
RETURN DISTINCT pub.id AS publication_id, pub.title AS title, pub.year AS year,
       pub.journal AS journal, pub.doi AS doi,
       coauthor.id AS partner_person_id
ORDER BY pub.year DESC
"""

T3E_BY_DEPT = """
MATCH (p:Person)-[:BELONGS_TO]->(:Department {id: $dept_id})
WHERE coalesce(p.is_itmo, false) = true
MATCH (p)-[:AUTHORED]->(pub:Publication)
WHERE pub.year >= coalesce($year_from, pub.year)
  AND pub.year <= coalesce($year_to, pub.year)
MATCH (coauthor:Person)-[:AUTHORED]->(pub)
WHERE coauthor.id <> p.id
WITH p, COUNT(DISTINCT coauthor.id) AS distinct_coauthors
RETURN p.id AS person_id, p.name_en AS name_en, p.name_ru AS name_ru,
       distinct_coauthors
ORDER BY distinct_coauthors DESC
"""

T3E_ITMO = """
MATCH (p:Person)
WHERE coalesce(p.is_itmo, false) = true
MATCH (p)-[:AUTHORED]->(pub:Publication)
WHERE pub.year >= coalesce($year_from, pub.year)
  AND pub.year <= coalesce($year_to, pub.year)
MATCH (coauthor:Person)-[:AUTHORED]->(pub)
WHERE coauthor.id <> p.id
WITH p, COUNT(DISTINCT coauthor.id) AS distinct_coauthors
RETURN p.id AS person_id, p.name_en AS name_en, p.name_ru AS name_ru,
       distinct_coauthors
ORDER BY distinct_coauthors DESC
"""

T4_BY_DEPT = """
MATCH (p:Person)-[:BELONGS_TO]->(d:Department {id: $dept_id})
WHERE coalesce(p.is_itmo, false) = true
MATCH (p)-[c:CONTRIBUTED_TO]->(r:Repository)
RETURN p.id AS person_id, p.name_en AS name_en, p.name_ru AS name_ru,
       r.id AS repo_id, r.name AS repo_name, r.url AS repo_url,
       r.stars_num AS stars, r.license AS license,
       r.last_updated AS last_updated, r.archived AS archived,
       r.is_fork AS is_fork, r.has_readme AS has_readme,
       r.language AS language, r.contributors_count AS contributors_count,
       c.role AS role
ORDER BY p.name_en
"""

T4_ITMO = """
MATCH (p:Person)
WHERE coalesce(p.is_itmo, false) = true
MATCH (p)-[c:CONTRIBUTED_TO]->(r:Repository)
RETURN p.id AS person_id, p.name_en AS name_en, p.name_ru AS name_ru,
       r.id AS repo_id, r.name AS repo_name, r.url AS repo_url,
       r.stars_num AS stars, r.license AS license,
       r.last_updated AS last_updated, r.archived AS archived,
       r.is_fork AS is_fork, r.has_readme AS has_readme,
       r.language AS language, r.contributors_count AS contributors_count,
       c.role AS role
ORDER BY p.name_en
"""

T5A_BY_DEPT = """
MATCH (p:Person)-[:BELONGS_TO]->(:Department {id: $dept_id})
WHERE coalesce(p.is_itmo, false) = true
MATCH (p)-[:AUTHORED]->(pub:Publication)<-[:IMPLEMENTS]-(r:Repository)
MATCH (p)-[:CONTRIBUTED_TO]->(r)
RETURN DISTINCT p.id AS person_id, p.name_en AS name_en, p.name_ru AS name_ru,
       pub.id AS pub_id, pub.title AS pub_title,
       r.id AS repo_id, r.name AS repo_name, r.url AS repo_url
"""

T5A_ITMO = """
MATCH (p:Person)
WHERE coalesce(p.is_itmo, false) = true
MATCH (p)-[:AUTHORED]->(pub:Publication)<-[:IMPLEMENTS]-(r:Repository)
MATCH (p)-[:CONTRIBUTED_TO]->(r)
RETURN DISTINCT p.id AS person_id, p.name_en AS name_en, p.name_ru AS name_ru,
       pub.id AS pub_id, pub.title AS pub_title,
       r.id AS repo_id, r.name AS repo_name, r.url AS repo_url
"""

T5B_BY_DEPT = """
MATCH (p:Person)-[:BELONGS_TO]->(:Department {id: $dept_id})
WHERE coalesce(p.is_itmo, false) = true
MATCH (p)-[:AUTHORED]->(pub:Publication)<-[:IMPLEMENTS]-(r:Repository)
WHERE NOT (p)-[:CONTRIBUTED_TO]->(r)
RETURN DISTINCT p.id AS person_id, p.name_en AS name_en, p.name_ru AS name_ru,
       pub.id AS pub_id, pub.title AS pub_title,
       r.id AS repo_id, r.name AS repo_name, r.url AS repo_url
"""

T5B_ITMO = """
MATCH (p:Person)
WHERE coalesce(p.is_itmo, false) = true
MATCH (p)-[:AUTHORED]->(pub:Publication)<-[:IMPLEMENTS]-(r:Repository)
WHERE NOT (p)-[:CONTRIBUTED_TO]->(r)
RETURN DISTINCT p.id AS person_id, p.name_en AS name_en, p.name_ru AS name_ru,
       pub.id AS pub_id, pub.title AS pub_title,
       r.id AS repo_id, r.name AS repo_name, r.url AS repo_url
"""

T5C_BY_DEPT = """
MATCH (p:Person)-[:BELONGS_TO]->(:Department {id: $dept_id})
WHERE coalesce(p.is_itmo, false) = true
MATCH (p)-[c:CONTRIBUTED_TO]->(r:Repository)
RETURN DISTINCT p.id AS person_id, p.name_en AS name_en, p.name_ru AS name_ru,
       r.id AS repo_id, r.name AS repo_name, r.url AS repo_url,
       r.stars_num AS stars, r.license AS license,
       r.last_updated AS last_updated, r.archived AS archived,
       r.is_fork AS is_fork, r.has_readme AS has_readme,
       r.language AS language, r.contributors_count AS contributors_count,
       c.role AS role
"""

T5C_ITMO = """
MATCH (p:Person)
WHERE coalesce(p.is_itmo, false) = true
MATCH (p)-[c:CONTRIBUTED_TO]->(r:Repository)
RETURN DISTINCT p.id AS person_id, p.name_en AS name_en, p.name_ru AS name_ru,
       r.id AS repository_id, r.name AS repo_name, r.url AS repo_url,
       r.stars_num AS stars, r.license AS license,
       r.last_updated AS last_updated, r.archived AS archived,
       r.is_fork AS is_fork, r.has_readme AS has_readme,
       r.language AS language, r.contributors_count AS contributors_count,
       c.role AS role
"""

T8 = """
MATCH (pub:Publication) WHERE pub.id IN $topic_ids
  AND pub.year >= coalesce($year_from, pub.year)
  AND pub.year <= coalesce($year_to, pub.year)
MATCH (p:Person)-[:AUTHORED]->(pub)
MATCH (p)-[:BELONGS_TO]->(:Department {id: $dept_id})
WHERE coalesce(p.is_itmo, false) = true
RETURN DISTINCT p.id AS person_id, p.name_en AS name_en, p.name_ru AS name_ru,
       collect(DISTINCT pub.id) AS pub_ids
ORDER BY size(pub_ids) DESC
"""

T9_IMPLEMENTS = """
MATCH (pub:Publication) WHERE pub.id IN $topic_ids
  AND pub.year >= coalesce($year_from, pub.year)
  AND pub.year <= coalesce($year_to, pub.year)
MATCH (r:Repository)-[:IMPLEMENTS]->(pub)
OPTIONAL MATCH (p:Person)-[:AUTHORED]->(pub)
WHERE coalesce(p.is_itmo, false) = true
RETURN DISTINCT pub.id AS publication_id, pub.title AS title, pub.year AS year,
       pub.doi AS doi, pub.openalex_url AS openalex_url,
       r.id AS repo_id, r.name AS repo_name, r.url AS repo_url,
       r.stars_num AS stars, r.license AS license,
       r.last_updated AS last_updated, r.archived AS archived,
       r.language AS language, r.contributors_count AS contributors_count,
       collect(DISTINCT p.id) AS itmo_authors
ORDER BY pub.year DESC
"""

T9_MENTIONS = """
MATCH (pub:Publication) WHERE pub.id IN $topic_ids
  AND pub.year >= coalesce($year_from, pub.year)
  AND pub.year <= coalesce($year_to, pub.year)
MATCH (pub)-[:MENTIONS_LINK]->(r:Repository)
OPTIONAL MATCH (p:Person)-[:AUTHORED]->(pub)
WHERE coalesce(p.is_itmo, false) = true
RETURN DISTINCT pub.id AS publication_id, pub.title AS title, pub.year AS year,
       pub.doi AS doi, pub.openalex_url AS openalex_url,
       r.id AS repo_id, r.name AS repo_name, r.url AS repo_url,
       r.stars_num AS stars, r.license AS license,
       r.last_updated AS last_updated, r.archived AS archived,
       r.language AS language, r.contributors_count AS contributors_count,
       collect(DISTINCT p.id) AS itmo_authors
ORDER BY pub.year DESC
"""

T10 = """
MATCH (pub:Publication) WHERE pub.id IN $topic_ids
  AND pub.year >= coalesce($year_from, pub.year)
  AND pub.year <= coalesce($year_to, pub.year)
MATCH (p:Person)-[:AUTHORED]->(pub)
WHERE coalesce(p.is_itmo, false) = true
WITH p, collect(DISTINCT pub.id) AS pub_ids
RETURN p.id AS publication_id, p.name_en AS name_en, p.name_ru AS name_ru,
       size(pub_ids) AS publication_count,
       pub_ids
ORDER BY publication_count DESC
"""

TEMPLATE_QUERIES: dict[tuple[str, str | None], Any] = {
    # --- graph templates ---
    ("1",  None):  {"by_dept": T1_BY_DEPT,  "itmo": T1_ITMO},
    ("2",  None):  {"by_dept": T2_BY_DEPT,  "itmo": T2_ITMO},
    ("3",  "3a"):  T3A,
    ("3",  "3b"):  T3B,
    ("3",  "3c"):  T3C,
    ("3",  "3d"):  T3D,
    ("3",  "3e"):  {
        "by_dept":          T3E_BY_DEPT,
        "itmo":             T3E_ITMO},
    ("4",  None):  {"by_dept": T4_BY_DEPT,  "itmo": T4_ITMO},
    ("5",  "5a"):  {"by_dept": T5A_BY_DEPT, "itmo": T5A_ITMO},
    ("5",  "5b"):  {"by_dept": T5B_BY_DEPT, "itmo": T5B_ITMO},
    ("5",  "5c"):  {"by_dept": T5C_BY_DEPT, "itmo": T5C_ITMO},
    # --- topic templates (delegated to topic_subgraph) ---
    ("6",  None):  None,  # pipeline calls topic_subgraph() directly
    ("7",  None):  None,  # pipeline calls topic_subgraph() directly
    # --- mixed templates ---
    ("8",  None):  T8,
    ("9",  None):  {"implements": T9_IMPLEMENTS, "mentions": T9_MENTIONS},
    ("10", None):  T10,
}


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
