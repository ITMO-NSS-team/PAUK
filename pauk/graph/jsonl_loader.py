"""Загрузка JSONL-выхода пайплайна (data_enrichment/run_conveyor.py) в Neo4j.

Порядок жёсткий: сначала ВСЕ узлы (по всем типам), потом ВСЕ связи. Если
связь ссылается на узел, которого нет — она просто не создастся (Cypher
MATCH отфильтрует), заглушку не создаём (см. client.py — вместо этого лог
через relationships_created).
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path

from .client import Neo4jClient, chunked
from .extract import NODE_REGISTRY, extract_node, extract_relationships

logger = logging.getLogger(__name__)

# Файл -> ключ в NODE_REGISTRY. publications.jsonl/repositories.jsonl/
# github_profiles.jsonl пайплайн пока не пишет — отсутствующий файл не
# ошибка, просто пропускается (см. load_jsonl_dir), поэтому регистрировать
# их здесь заранее безопасно и бесплатно.
FILE_SPECS: dict[str, str] = {
    "departments.jsonl": "department",
    "persons.jsonl": "itmo_person",
    "publications.jsonl": "publication",
    "repositories.jsonl": "repository",
    "github_profiles.jsonl": "github_profile",
}


def _read_jsonl(path: Path) -> Iterator[dict]:
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


def extract_repo_links(
    pub_links_row: dict, known_repository_urls: set[str]
) -> tuple[list[tuple[str, dict]], list[tuple[str, str, dict]]]:
    """repo_links.jsonl (PubLinks) не узел, а список кандидатов ссылок на код
    для одной публикации, без target_kind в отличие от Publication.mentions_links.

    Правило (как в старом graph_loader.py): url совпал с уже известным
    Repository.url -> ребро на Repository (матч по url); иначе -> ребро на
    LinkCandidate, и сам узел LinkCandidate тут же создаём (id = url — других
    стабильных id для кандидата тут нет).

    -> (link_candidate_nodes, mentions_link_edges)
    """
    publication_id = pub_links_row["publication_id"]
    candidate_nodes: list[tuple[str, dict]] = []
    edges: list[tuple[str, str, dict]] = []

    for link in pub_links_row.get("links") or []:
        url = link.get("url")
        if not url:
            continue
        props = {
            k: link[k]
            for k in ("context", "page_number", "is_relevant", "llm_confidence", "llm_reason")
            if link.get(k) is not None
        }
        if url in known_repository_urls:
            edges.append((publication_id, url, props))  # tgt matched by "url"
        else:
            candidate_nodes.append((url, {"url": url, "host": link.get("host")}))
            edges.append((publication_id, url, props))  # tgt matched by "id" == url

    return candidate_nodes, edges


def load_jsonl_dir(client: Neo4jClient, in_dir: Path) -> None:
    node_batches: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    rel_batches: dict[tuple[str, str, str, str], list[tuple[str, str, dict]]] = defaultdict(list)
    known_repository_urls: set[str] = set()

    for filename, spec_key in FILE_SPECS.items():
        path = in_dir / filename
        if not path.exists():
            logger.info("%s не найден в %s, пропускаю", filename, in_dir)
            continue
        spec = NODE_REGISTRY[spec_key]
        for row in _read_jsonl(path):
            labels, node = extract_node(row, spec)
            node_batches[labels].append(node)
            if spec_key == "repository":
                known_repository_urls.add(row.get("url"))
            for key, rels in extract_relationships(row, spec).items():
                rel_batches[key].extend(rels)

    repo_links_path = in_dir / "repo_links.jsonl"
    if repo_links_path.exists():
        mentions_key = ("Publication", "LinkCandidate", "MENTIONS_LINK", "id")
        mentions_repo_key = ("Publication", "Repository", "MENTIONS_LINK", "url")
        for row in _read_jsonl(repo_links_path):
            candidate_nodes, edges = extract_repo_links(row, known_repository_urls)
            node_batches["LinkCandidate"].extend(candidate_nodes)
            for src_id, tgt_id, props in edges:
                key = mentions_repo_key if tgt_id in known_repository_urls else mentions_key
                rel_batches[key].append((src_id, tgt_id, props))
    else:
        logger.info("repo_links.jsonl не найден в %s, пропускаю", in_dir)

    for labels, nodes in node_batches.items():
        for chunk in chunked(nodes):
            client.upsert_nodes_batch(labels, chunk)
        logger.info("узлы (:%s): загружено %d", labels, len(nodes))

    for (src_label, tgt_label, rel_type, tgt_match_prop), rels in rel_batches.items():
        for chunk in chunked(rels):
            client.upsert_relationships_batch(src_label, tgt_label, rel_type, chunk, tgt_match_prop)
        logger.info("связи (:%s)-[:%s]->(:%s): запрошено %d", src_label, rel_type, tgt_label, len(rels))
