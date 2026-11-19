"""Export the authorship and department edges used to judge independence."""

from __future__ import annotations

import json
import os
from pathlib import Path

from neo4j import GraphDatabase

OUT = Path("/analysis/forks/data")
OUT.mkdir(parents=True, exist_ok=True)

driver = GraphDatabase.driver(
    os.environ.get("NEO4J_URI", "bolt://neo4j:7687"),
    auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "")),
)


def dump(name: str, query: str) -> None:
    with driver.session() as session:
        rows = [dict(record) for record in session.run(query)]
    path = OUT / f"{name}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    print(f"{name}: {len(rows)} rows")


dump(
    "authored",
    "MATCH (p:Person)-[:AUTHORED]->(pub:Publication) "
    "RETURN p.id AS person_id, pub.id AS publication_id",
)
dump(
    "person_departments",
    "MATCH (p:Person)-[:BELONGS_TO]->(d:Department) "
    "RETURN p.id AS person_id, d.id AS department_id, d.name AS department",
)
dump(
    "departments",
    "MATCH (d:Department) RETURN d.id AS id, d.name AS name",
)
driver.close()
