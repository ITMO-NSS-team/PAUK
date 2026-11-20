"""Dump the graph and document data the fork analysis needs.

Read-only: the script touches neither MongoDB nor Neo4j beyond queries.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pymongo
from neo4j import GraphDatabase

OUT = Path(__file__).resolve().parent / "data"
OUT.mkdir(parents=True, exist_ok=True)


def dump(name: str, rows: list[dict]) -> None:
    path = OUT / f"{name}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    print(f"{name}: {len(rows)} rows -> {path}")


def mongo_rows(db, name: str, projection: dict, query: dict | None = None) -> list[dict]:
    return list(db[name].find(query or {}, projection))


mongo = pymongo.MongoClient(os.environ["MONGO_URI"], serverSelectionTimeoutMS=15000)
db = mongo[os.environ.get("MONGO_DB", "pauk_2026_09_21")]

repos = mongo_rows(db, "repositories", {
    "_id": 0, "id": 1, "name": 1, "url": 1, "owner_login": 1, "is_fork": 1,
    "forks_num": 1, "stars_num": 1, "language": 1, "archived": 1, "description": 1,
    "contributors": 1, "publication_ids": 1, "cited_urls": 1, "last_updated": 1,
    "has_readme": 1, "license": 1,
})
dump("repositories", repos)

persons = mongo_rows(db, "persons", {
    "_id": 0, "id": 1, "name_en": 1, "name_raw": 1, "emails": 1, "department_ids": 1,
})
dump("persons", persons)

pubs = mongo_rows(db, "publications", {
    "_id": 0, "id": 1, "year": 1, "title": 1, "doi": 1, "has_code": 1,
    "publication_date": 1,
})
dump("publications", pubs)

links = list(db.repo_links.find({}, {"_id": 0, "publication_id": 1, "links": 1}))
flat = []
for row in links:
    for link in row.get("links") or []:
        flat.append({
            "publication_id": row.get("publication_id"),
            "url": link.get("url"),
            "is_relevant": link.get("is_relevant"),
            "host": link.get("host"),
        })
dump("repo_links", flat)

profiles = list(db.github_profiles.find({}, {"_id": 0}))
dump("github_profiles", profiles)

driver = GraphDatabase.driver(
    os.environ.get("NEO4J_URI", "bolt://neo4j:7687"),
    auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "")),
)


def graph_rows(query: str, **params) -> list[dict]:
    with driver.session() as session:
        return [dict(record) for record in session.run(query, **params)]


graph_repos = graph_rows(
    """
    MATCH (r:Repository)
    OPTIONAL MATCH (r)-[:OWNED_BY]->(g:GitHubProfile)
    RETURN r.id AS id, r.url AS url, r.created_at AS created_at,
           r.last_updated AS last_updated, r.forks_num AS forks_num,
           r.stars_num AS stars_num, r.is_fork AS is_fork, r.language AS language,
           r.archived AS archived, g.login AS owner_login, g.type AS owner_type
    """
)
dump("graph_repositories", graph_repos)

graph_persons = graph_rows(
    "MATCH (p:Person) WHERE p.github IS NOT NULL RETURN p.id AS id, p.github AS github"
)
dump("graph_persons_github", graph_persons)

graph_profiles = graph_rows(
    "MATCH (g:GitHubProfile) RETURN g.login AS login, g.type AS type, "
    "g.company AS company, g.location AS location, g.name AS name"
)
dump("graph_profiles", graph_profiles)

implements = graph_rows(
    "MATCH (r:Repository)-[:IMPLEMENTS]->(p:Publication) "
    "RETURN r.id AS repo_id, p.id AS publication_id, p.year AS year"
)
dump("implements", implements)

mentions = graph_rows(
    "MATCH (p:Publication)-[m:MENTIONS_LINK]->(x) "
    "RETURN p.id AS publication_id, x.id AS target_id, labels(x)[0] AS target_label, "
    "m.is_relevant AS is_relevant, m.classification_status AS classification_status, "
    "m.llm_confidence AS llm_confidence, m.llm_reason AS llm_reason, "
    "m.candidate_urls AS candidate_urls, m.context AS context, m.page_number AS page_number"
)
dump("mentions", mentions)

print("mongo db:", db.name)
driver.close()
