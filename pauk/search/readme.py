"""Download repository READMEs from GitHub into the local data dir.

The graph keeps only `has_readme`; the text is what makes a repository
searchable by meaning. Nothing is written to Neo4j or Mongo.
"""

from __future__ import annotations

import logging

from pauk.settings import Settings
from pauk.sources.base import HttpRequestError
from pauk.sources.github import GitHubClient
from pauk.urls import github_owner_name

from .index import readme_path
from .reader import GraphReader

logger = logging.getLogger(__name__)

REPOSITORIES = "MATCH (r:Repository) WHERE r.has_readme RETURN r.id AS id, r.url AS url ORDER BY r.id"


def fetch_all(config: Settings, *, force: bool = False) -> dict[str, int]:
    graph = GraphReader(config)
    try:
        rows = graph.read(REPOSITORIES)
    finally:
        graph.close()
    client = GitHubClient(config.request_timeout, config.github_token)
    stats = {"fetched": 0, "cached": 0, "missing": 0, "failed": 0}
    for row in rows:
        path = readme_path(config, row["id"])
        if path.exists() and not force:
            stats["cached"] += 1
            continue
        parsed = github_owner_name(row["url"])
        if parsed is None:
            stats["failed"] += 1
            continue
        try:
            text = client.get_readme(*parsed)
        except HttpRequestError as exc:
            logger.warning("readme %s/%s: %s", *parsed, exc)
            stats["failed"] += 1
            continue
        if text is None:
            stats["missing"] += 1
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        stats["fetched"] += 1
    return stats
