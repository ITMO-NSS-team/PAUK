"""Mock external clients backed by the synthetic universe, plus an
in-memory Neo4j stand-in that records what the loader would upsert."""

from __future__ import annotations

from collections import defaultdict

import requests

from pauk.graph.client import _merge_duplicate_properties


def _http_404(url: str) -> requests.HTTPError:
    return requests.HTTPError(f"404 Client Error: Not Found for url: {url}")


class MockOpenAlexClient:
    def __init__(self, universe: dict) -> None:
        self.works = {w["id"].rsplit("/", 1)[-1]: w for w in universe["works"]}
        # Complete records the single-work endpoint serves for works whose
        # list payload is truncated.
        self.works_full = dict(universe.get("works_full") or {})
        self.authors_api = universe["authors_api"]

    @staticmethod
    def normalize_work_id(work_id: str) -> str:
        return work_id.rstrip("/").split("/")[-1].upper()

    def get_work(self, work_id: str) -> dict:
        normalized = self.normalize_work_id(work_id)
        if normalized in self.works_full:
            return self.works_full[normalized]
        if normalized not in self.works:
            raise _http_404(f"https://api.openalex.org/works/{normalized}")
        return self.works[normalized]

    def iter_works(self, ror_id: str, date_from: str, date_to: str):
        yield from self.works.values()

    def get_author(self, author_id: str) -> dict:
        normalized = author_id.rstrip("/").split("/")[-1].upper()
        payload = self.authors_api.get(normalized)
        if payload is None:
            raise _http_404(f"https://api.openalex.org/authors/{normalized}")
        return payload


class MockGitHubClient:
    def __init__(self, universe: dict) -> None:
        self.repos = universe["github"]
        self.renamed = universe["renamed"]
        self.calls: list[tuple[str, str]] = []

    def get_repository(self, owner: str, name: str) -> dict:
        self.calls.append((owner, name))
        key = (owner.lower(), name.lower())
        key = self.renamed.get(key, key)
        payload = self.repos.get(key)
        if payload is None:
            raise _http_404(f"https://api.github.com/repos/{owner}/{name}")
        return payload

    def has_readme(self, owner: str, name: str) -> bool:
        return True


class MockCrossrefClient:
    def __init__(self, universe: dict) -> None:
        self.works = universe["crossref"]

    def get_work(self, doi: str) -> dict:
        clean = doi.removeprefix("https://doi.org/").removeprefix("http://dx.doi.org/")
        if clean not in self.works:
            raise _http_404(f"https://api.crossref.org/works/{clean}")
        return self.works[clean]


class MockOrcidClient:
    def __init__(self, universe: dict) -> None:
        self.records = universe["orcid"]

    def get_record(self, orcid: str) -> dict:
        if orcid not in self.records:
            raise _http_404(f"https://pub.orcid.org/v3.0/{orcid}/record")
        return self.records[orcid]


class UnexpectedNetworkClient:
    """Any call means a stage tried the network although it shouldn't have."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __getattr__(self, name: str):
        raise AssertionError(f"unexpected external call: {name}")


class RecordingNeo4jClient:
    """In-memory double of Neo4jClient with just enough MERGE semantics.

    Nodes are stored per primary label; persons follow the sticky-:Itmo rule
    of upsert_person_nodes_batch. Relationships resolve their endpoints the
    way the Cypher MATCH would; anything unresolvable is recorded in
    `unresolved` so tests can assert it never happens.
    """

    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, dict]] = defaultdict(dict)
        self.person_labels: dict[str, str] = {}
        self.edges: dict[tuple[str, str, str, str, str], dict] = {}
        self.unresolved: list[tuple[str, str, str, str, str]] = []

    # --- Neo4jClient interface -------------------------------------------------
    def upsert_nodes_batch(self, labels, nodes) -> None:
        label_str = ":".join(labels) if isinstance(labels, list) else labels
        primary = label_str.split(":")[0]
        for node_id, props in nodes:
            clean = {k: v for k, v in props.items() if k not in ("id", "created_at", "updated_at")}
            self.nodes[primary].setdefault(node_id, {}).update(clean)

    def upsert_person_nodes_batch(self, nodes, is_itmo: bool) -> None:
        for node_id, props in nodes:
            clean = {k: v for k, v in props.items() if k not in ("id", "created_at", "updated_at")}
            self.nodes["Person"].setdefault(node_id, {}).update(clean)
            if is_itmo:
                self.person_labels[node_id] = "Itmo"
            else:
                self.person_labels.setdefault(node_id, "External")

    def upsert_relationships_batch(self, src_label, tgt_label, rel_type, relationships,
                                   tgt_match_prop: str = "id") -> int:
        src_primary = src_label.split(":")[0]
        tgt_primary = tgt_label.split(":")[0]
        matched = 0
        for src_id, tgt_id, props in relationships:
            src_ok = src_id in self.nodes.get(src_primary, {})
            if tgt_match_prop == "id":
                tgt_ok = tgt_id in self.nodes.get(tgt_primary, {})
            else:
                tgt_ok = any(node.get(tgt_match_prop) == tgt_id
                             for node in self.nodes.get(tgt_primary, {}).values())
            if src_ok and tgt_ok:
                key = (src_primary, rel_type, tgt_primary, src_id, tgt_id)
                self.edges.setdefault(key, {}).update(props)
                matched += 1
            else:
                self.unresolved.append((src_label, rel_type, tgt_label, src_id, tgt_id))
        return matched

    def promote_link_candidates_batch(self, candidates) -> None:
        """Mirror of Neo4jClient.promote_link_candidates_batch: move
        MENTIONS_LINK edges from a LinkCandidate to the now-known Repository
        (matched by url) and delete the candidate node."""
        for candidate_id, repository_url in candidates:
            if candidate_id not in self.nodes.get("LinkCandidate", {}):
                continue
            repo_exists = any(props.get("url") == repository_url
                              for props in self.nodes.get("Repository", {}).values())
            if not repo_exists:
                continue
            moved = {}
            for key, props in list(self.edges.items()):
                _src, rel_type, tgt_primary, src_id, tgt_id = key
                if tgt_primary == "LinkCandidate" and tgt_id == candidate_id and rel_type == "MENTIONS_LINK":
                    del self.edges[key]
                    moved[("Publication", "MENTIONS_LINK", "Repository", src_id, repository_url)] = props
            for key, props in moved.items():
                self.edges.setdefault(key, {}).update(props)
            del self.nodes["LinkCandidate"][candidate_id]

    def _fold_nodes(self, label: str, merges) -> int:
        """Mirror of Neo4jClient._fold_nodes_batch: move the duplicate node's
        edges (both directions) onto the canonical node and delete the
        duplicate. On both nodes and edges the canonical's own values win,
        but anything only the duplicate knew fills the gap."""
        removed = 0
        for dup_id, canonical_id in merges:
            if dup_id == canonical_id:
                continue
            if dup_id not in self.nodes.get(label, {}):
                continue
            if canonical_id not in self.nodes.get(label, {}):
                continue
            for key, props in list(self.edges.items()):
                src_primary, rel_type, tgt_primary, src_id, tgt_id = key
                if src_primary == label and src_id == dup_id:
                    moved_key = (src_primary, rel_type, tgt_primary, canonical_id, tgt_id)
                elif tgt_primary == label and tgt_id == dup_id:
                    moved_key = (src_primary, rel_type, tgt_primary, src_id, canonical_id)
                else:
                    continue
                del self.edges[key]
                self.edges[moved_key] = {**props, **self.edges.get(moved_key, {})}
            dup_props = self.nodes[label].pop(dup_id)
            canonical_props = self.nodes[label][canonical_id]
            canonical_props.update(_merge_duplicate_properties(label, canonical_props, dup_props))
            if label == "Person":
                self.person_labels.pop(dup_id, None)
            removed += 1
        return removed

    def fetch_persons_for_dedup(self) -> list[dict]:
        """Mirror of Neo4jClient.fetch_persons_for_dedup over recorded state."""
        rows = []
        for person_id, props in self.nodes.get("Person", {}).items():
            rows.append({
                "id": person_id,
                **{field: props.get(field) for field in (
                    "openalex_id", "name_en", "name_variants", "orcid", "email",
                    "github", "openreview", "google_scholar", "merged_ids")},
                "is_itmo": self.person_labels.get(person_id) == "Itmo",
                "publication_ids": sorted({
                    tgt_id for (src_primary, rel_type, _tgt, src_id, tgt_id) in self.edges
                    if src_primary == "Person" and rel_type == "AUTHORED" and src_id == person_id
                }),
                "department_ids": sorted({
                    tgt_id for (src_primary, rel_type, _tgt, src_id, tgt_id) in self.edges
                    if src_primary == "Person" and rel_type == "BELONGS_TO" and src_id == person_id
                }),
            })
        return rows

    def fetch_publication_fields(self) -> dict[str, set[str]]:
        """Mirror of Neo4jClient.fetch_publication_fields."""
        return {
            publication_id: set(props["fields"])
            for publication_id, props in self.nodes.get("Publication", {}).items()
            if props.get("fields")
        }

    def fetch_publications_for_dedup(self) -> list[dict]:
        """Mirror of Neo4jClient.fetch_publications_for_dedup."""
        rows = []
        for publication_id, props in self.nodes.get("Publication", {}).items():
            authors = [
                {"person_id": src_id,
                 "name": self.nodes.get("Person", {}).get(src_id, {}).get("name_en"),
                 "position": edge_props.get("position")}
                for (_src, rel_type, tgt_primary, src_id, tgt_id), edge_props in self.edges.items()
                if rel_type == "AUTHORED" and tgt_primary == "Publication" and tgt_id == publication_id
            ]
            rows.append({
                "id": publication_id,
                **{field: props.get(field) for field in (
                    "type", "doi", "title", "journal", "publication_date", "year",
                    "openalex_url", "pdf_url", "abstract", "versions", "merged_ids")},
                "author_count": len(authors),
                "authors": authors,
            })
        return rows

    def fetch_repositories_for_dedup(self) -> list[dict]:
        """Mirror of Neo4jClient.fetch_repositories_for_dedup."""
        return [{
            "id": repository_id,
            **{field: props.get(field) for field in (
                "url", "github_id", "cited_urls", "access_date", "merged_ids")},
            "publication_count": sum(
                1 for (src_primary, rel_type, _tgt, src_id, _tid) in self.edges
                if rel_type == "IMPLEMENTS" and src_primary == "Repository" and src_id == repository_id),
        } for repository_id, props in self.nodes.get("Repository", {}).items()]

    def fetch_merged_id_map(self, label: str) -> dict[str, str]:
        """Mirror of Neo4jClient.fetch_merged_id_map over recorded state."""
        return {
            merged_id: node_id
            for node_id, props in self.nodes.get(label, {}).items()
            for merged_id in props.get("merged_ids") or []
        }

    def merge_person_nodes_batch(self, merges) -> int:
        return self._fold_nodes("Person", merges)

    def merge_publication_nodes_batch(self, merges) -> int:
        return self._fold_nodes("Publication", merges)

    def merge_repository_nodes_batch(self, merges) -> int:
        return self._fold_nodes("Repository", merges)

    def close(self) -> None:
        pass

    # --- assertion helpers -------------------------------------------------------
    def edge_pairs(self, rel_type: str) -> set[tuple[str, str]]:
        return {(src_id, tgt_id) for (_, rel, _, src_id, tgt_id) in self.edges if rel == rel_type}

    def snapshot(self):
        return (
            {label: dict(items) for label, items in self.nodes.items()},
            dict(self.person_labels),
            set(self.edges),
        )


class MockOpenRouterClient:
    """A fixed link-relevance verdict, so the bench never calls a real model.

    Every link is judged the authors' own. The bench measures structure — how
    many edges of each kind the pipeline builds — and a live model would make
    those counts depend on its mood: run against a real endpoint, this
    universe's synthetic URLs are judged someone else's tool 81 times out of
    86, and the IMPLEMENTS count moves with the model. Whether the judgment
    itself is right is settled in tests/unit/test_stages.py.
    """

    def __init__(self, *args, **kwargs) -> None:
        self.last_error = None
        self.last_response = None
        self.last_usage = None
        self.calls: list[str] = []

    def chat_json(self, prompt: str) -> dict:
        self.calls.append(prompt)
        self.last_response = '{"is_authors_artifact": true}'
        return {"is_authors_artifact": True, "confidence": 1.0,
                "reason": "bench: fixed verdict"}
