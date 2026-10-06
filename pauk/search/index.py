"""Build the search indexes from the graph into local files.

Everything is written to `<data_dir>/search`, never to Neo4j: a vector
property and a vector index would change the graph, and the next `pauk
publish` or `dedup graph` could drop or fold them anyway. At ~14k
publications a numpy matrix in memory is as fast as a vector index.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from pauk.settings import Settings

from .reader import GraphReader
from .retrieval import load_model

logger = logging.getLogger(__name__)

ABSTRACT_CHARS = 3000
README_CHARS = 4000

PUBLICATIONS = """
MATCH (p:Publication)
RETURN p.id AS id, p.title AS title, p.abstract AS abstract, p.fields AS fields, p.year AS year, p.type AS type
ORDER BY p.id
"""
# Records that are not research texts: reviews of someone else's paper and
# book front matter. They stay in the graph, only search does not offer them.
NOT_SEARCHABLE_TYPES = {"paratext", "peer-review"}
# Without an abstract these types are almost always a single figure or table
# of an article (OpenAlex gives them their own DOI); a bare caption like
# "Graphical Abstract" is close to any vague query and wins it.
FIGURE_LIKE_TYPES = {"other", "dataset"}
_PARATEXT_TITLE = re.compile(
    r"^(graphic(al)? abstract|contributors?|preface|foreword|front ?matter|back ?matter|"
    r"table of contents|editorial board|untitled)\.?$",
    re.IGNORECASE,
)
REPOSITORIES = """
MATCH (r:Repository)
RETURN r.id AS id, r.name AS name, r.url AS url, r.description AS description, r.topics AS topics
ORDER BY r.id
"""
PERSONS = """
MATCH (p:Person)
RETURN p.id AS id, p.name_en AS name_en, p.name_ru AS name_ru, p.name_raw AS name_raw,
       p.name_variants AS name_variants,
       p.other_names AS other_names, p.surname_ru AS surname_ru, p.first_name_ru AS first_name_ru,
       p.second_name_ru AS second_name_ru, coalesce(p.is_itmo, false) AS is_itmo,
       COUNT { (p)-[:AUTHORED]->() } AS publications
ORDER BY p.id
"""
# The graph also holds ~120 unconnected `dept_<hash>` nodes left over from an
# older department scheme; they share names with the real units, and resolving
# a name to one of them would answer with an empty profile.
DEPARTMENTS = """
MATCH (d:Department) WHERE EXISTS { (d)--() }
RETURN d.id AS id, d.name_ru AS name_ru, d.name_en AS name_en, d.name_variants AS name_variants,
       d.context_aliases AS context_aliases, d.kind AS kind,
       COUNT { (:Publication)-[:PRODUCED_BY]->(d) } AS publications
ORDER BY d.id
"""


def is_searchable(row: dict[str, Any]) -> bool:
    if row.get("type") in NOT_SEARCHABLE_TYPES:
        return False
    if (row.get("abstract") or "").strip():
        return True
    return row.get("type") not in FIGURE_LIKE_TYPES and not _PARATEXT_TITLE.match((row.get("title") or "").strip())


def publication_text(row: dict[str, Any]) -> str:
    title = (row.get("title") or "").strip()
    abstract = (row.get("abstract") or "").strip()[:ABSTRACT_CHARS]
    if abstract:
        return f"{title}. {abstract}"
    # Without an abstract the OpenAlex fields are the only hint of the topic.
    fields = ", ".join(row.get("fields") or [])
    return f"{title}. {fields}" if fields else title


def repository_text(row: dict[str, Any], readme: str | None) -> str:
    parts = [row.get("name") or "", row.get("description") or "", " ".join(row.get("topics") or [])]
    if readme:
        parts.append(readme[:README_CHARS])
    return ". ".join(part.strip() for part in parts if part and part.strip())


def readme_path(config: Settings, repository_id: str) -> Path:
    return config.search_dir / "readmes" / f"{repository_id}.md"


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def build(config: Settings, *, batch_size: int = 16) -> dict[str, int]:
    graph = GraphReader(config)
    try:
        publications = graph.read(PUBLICATIONS)
        repositories = graph.read(REPOSITORIES)
        persons = graph.read(PERSONS)
        departments = graph.read(DEPARTMENTS)
    finally:
        graph.close()

    found = len(publications)
    publications = [row for row in publications if is_searchable(row)]
    pub_texts = [publication_text(row) for row in publications]
    repo_texts = []
    with_readme = 0
    for row in repositories:
        path = readme_path(config, row["id"])
        readme = path.read_text(encoding="utf-8") if path.exists() else None
        with_readme += bool(readme)
        repo_texts.append(repository_text(row, readme))

    logger.info("embedding %d publications and %d repositories with %s",
                len(pub_texts), len(repo_texts), config.search_embedding_model)
    model = load_model(config.search_embedding_model)
    encode = dict(batch_size=batch_size, normalize_embeddings=True, show_progress_bar=True, convert_to_numpy=True)
    pub_vectors = model.encode(pub_texts, **encode).astype(np.float32)
    repo_vectors = model.encode(repo_texts, **encode).astype(np.float32)

    out = config.search_dir
    out.mkdir(parents=True, exist_ok=True)
    np.save(out / "publications.npy", pub_vectors)
    np.save(out / "repositories.npy", repo_vectors)
    _write_json(out / "publications.json", [
        {"id": row["id"], "year": row.get("year"), "title": row.get("title"), "text": text}
        for row, text in zip(publications, pub_texts, strict=True)
    ])
    _write_json(out / "repositories.json", [
        {"id": row["id"], "name": row.get("name"), "url": row.get("url"), "text": text}
        for row, text in zip(repositories, repo_texts, strict=True)
    ])
    _write_json(out / "persons.json", persons)
    _write_json(out / "departments.json", departments)
    manifest = {
        "built_at": datetime.now(UTC).isoformat(),
        "embedding_model": config.search_embedding_model,
        "publications": len(publications),
        "publications_not_searchable": found - len(publications),
        "publications_with_abstract": sum(bool(row.get("abstract")) for row in publications),
        "repositories": len(repositories),
        "repositories_with_readme": with_readme,
        "persons": len(persons),
        "departments": len(departments),
        "dimensions": int(pub_vectors.shape[1]) if len(pub_vectors) else 0,
    }
    _write_json(out / "manifest.json", manifest)
    return manifest
