"""Mock external clients backed by the synthetic universe, plus an
in-memory Neo4j stand-in that records what the loader would upsert."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy

import requests


def _http_404(url: str) -> requests.HTTPError:
    return requests.HTTPError(f"404 Client Error: Not Found for url: {url}")


def _http_429(url: str) -> requests.HTTPError:
    return requests.HTTPError(f"429 Client Error: Too Many Requests for url: {url}")


def _http_503(url: str) -> requests.HTTPError:
    return requests.HTTPError(f"503 Server Error: Service Unavailable for url: {url}")


class MockOpenAlexClient:
    def __init__(self, universe: dict) -> None:
        self.works = {w["id"].rsplit("/", 1)[-1]: w for w in universe["works"]}
        self.authors_api = universe["authors_api"]

    @staticmethod
    def normalize_work_id(work_id: str) -> str:
        return work_id.rstrip("/").split("/")[-1].upper()

    def get_work(self, work_id: str) -> dict:
        normalized = self.normalize_work_id(work_id)
        if normalized not in self.works:
            raise _http_404(f"https://api.openalex.org/works/{normalized}")
        return self.works[normalized]

    def iter_works(self, ror_id: str, date_from: str, date_to: str):
        """Works of one publication period, the way the API filter would.

        Works without a publication date have nothing to filter on and are
        served regardless — the pipeline has to cope with them either way.
        """
        for work in self.works.values():
            published = work.get("publication_date")
            if published is None or date_from <= published <= date_to:
                yield work

    def get_author(self, author_id: str) -> dict:
        normalized = author_id.rstrip("/").split("/")[-1].upper()
        payload = self.authors_api.get(normalized)
        if payload is None:
            raise _http_404(f"https://api.openalex.org/authors/{normalized}")
        return payload


class MockGitHubClient:
    """GitHub, optionally rate-limiting some repositories on first contact.

    `rate_limited_once` holds (owner, name) keys in lower case; the first
    call for such a key answers 429 and every later one succeeds, which is
    how a transient failure looks to the repositories stage: one run leaves
    the row FAILED, the next one completes it.
    """

    def __init__(self, universe: dict, rate_limited_once: frozenset = frozenset()) -> None:
        self.repos = universe["github"]
        self.renamed = universe["renamed"]
        self.calls: list[tuple[str, str]] = []
        self.rate_limited_once = set(rate_limited_once)

    def get_repository(self, owner: str, name: str) -> dict:
        self.calls.append((owner, name))
        key = (owner.lower(), name.lower())
        if key in self.rate_limited_once:
            self.rate_limited_once.discard(key)
            raise _http_429(f"https://api.github.com/repos/{owner}/{name}")
        key = self.renamed.get(key, key)
        payload = self.repos.get(key)
        if payload is None:
            raise _http_404(f"https://api.github.com/repos/{owner}/{name}")
        return payload


class MockCrossrefClient:
    def __init__(self, universe: dict) -> None:
        self.works = universe["crossref"]

    def get_work(self, doi: str) -> dict:
        clean = doi.removeprefix("https://doi.org/").removeprefix("http://dx.doi.org/")
        if clean not in self.works:
            raise _http_404(f"https://api.crossref.org/works/{clean}")
        return self.works[clean]


class MockOrcidClient:
    """ORCID, optionally unavailable for some records on first contact.

    Same shape as MockGitHubClient's rate limit: the first request for an
    id in `unavailable_once` fails, so the persons stage records FAILED and
    a later run heals the row.
    """

    def __init__(self, universe: dict, unavailable_once: frozenset = frozenset()) -> None:
        self.records = universe["orcid"]
        self.unavailable_once = set(unavailable_once)

    def get_record(self, orcid: str) -> dict:
        if orcid in self.unavailable_once:
            self.unavailable_once.discard(orcid)
            raise _http_503(f"https://pub.orcid.org/v3.0/{orcid}/record")
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

    def _resolve(self, label: str, match_prop: str, value: str) -> str | None:
        """Node id the way Cypher's MATCH would find it.

        `tgt_match_prop` is a lookup key, not part of the relationship: a
        MENTIONS_LINK found by Repository.url still hangs off the node, so a
        later rename or a fold of that node keeps it.
        """
        nodes = self.nodes.get(label, {})
        if match_prop == "id":
            return value if value in nodes else None
        return next((node_id for node_id, props in nodes.items()
                     if props.get(match_prop) == value), None)

    def _has_edges(self, label: str, node_id: str) -> bool:
        return any((src_primary == label and src_id == node_id)
                   or (tgt_primary == label and tgt_id == node_id)
                   for (src_primary, _rel, tgt_primary, src_id, tgt_id) in self.edges)

    def upsert_relationships_batch(self, src_label, tgt_label, rel_type, relationships,
                                   tgt_match_prop: str = "id") -> int:
        src_primary = src_label.split(":")[0]
        tgt_primary = tgt_label.split(":")[0]
        matched = 0
        for src_id, tgt_value, props in relationships:
            tgt_id = self._resolve(tgt_primary, tgt_match_prop, tgt_value)
            if src_id in self.nodes.get(src_primary, {}) and tgt_id is not None:
                key = (src_primary, rel_type, tgt_primary, src_id, tgt_id)
                self.edges.setdefault(key, {}).update(props)
                matched += 1
            else:
                self.unresolved.append((src_label, rel_type, tgt_label, src_id, tgt_value))
        return matched

    def promote_link_candidates_batch(self, candidates) -> None:
        """Mirror of Neo4jClient.promote_link_candidates_batch: move
        MENTIONS_LINK edges from a LinkCandidate to the now-known Repository
        (matched by url) and delete the candidate once nothing points at it."""
        for candidate_id, repository_url in candidates:
            if candidate_id not in self.nodes.get("LinkCandidate", {}):
                continue
            repository_id = self._resolve("Repository", "url", repository_url)
            if repository_id is None:
                continue
            moved = {}
            for key, props in list(self.edges.items()):
                _src, rel_type, tgt_primary, src_id, tgt_id = key
                if tgt_primary == "LinkCandidate" and tgt_id == candidate_id and rel_type == "MENTIONS_LINK":
                    del self.edges[key]
                    moved[("Publication", "MENTIONS_LINK", "Repository", src_id, repository_id)] = props
            for key, props in moved.items():
                self.edges.setdefault(key, {}).update(props)
            if not self._has_edges("LinkCandidate", candidate_id):
                del self.nodes["LinkCandidate"][candidate_id]

    def _fold_nodes(self, label: str, merges) -> int:
        """Mirror of Neo4jClient._fold_nodes_batch: move the duplicate node's
        edges (both directions) onto the canonical node — existing canonical
        edges win — and delete the duplicate node."""
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
                    del self.edges[key]
                    self.edges.setdefault(
                        (src_primary, rel_type, tgt_primary, canonical_id, tgt_id), props)
                elif tgt_primary == label and tgt_id == dup_id:
                    del self.edges[key]
                    self.edges.setdefault(
                        (src_primary, rel_type, tgt_primary, src_id, canonical_id), props)
            del self.nodes[label][dup_id]
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
            })
        return rows

    def fetch_publications_for_dedup(self) -> list[dict]:
        """Mirror of Neo4jClient.fetch_publications_for_dedup."""
        return [{
            "id": publication_id,
            **{field: props.get(field) for field in (
                "doi", "title", "publication_date", "merged_ids")},
            "author_count": sum(
                1 for (_src, rel_type, tgt_primary, _sid, tgt_id) in self.edges
                if rel_type == "AUTHORED" and tgt_primary == "Publication" and tgt_id == publication_id),
        } for publication_id, props in self.nodes.get("Publication", {}).items()]

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

    def edge_props(self, rel_type: str, src_id: str, tgt_id: str) -> dict:
        """Properties of one relationship, whatever its endpoint labels."""
        for (_src, rel, _tgt, src, tgt), props in self.edges.items():
            if rel == rel_type and src == src_id and tgt == tgt_id:
                return props
        raise AssertionError(f"no {rel_type} edge {src_id} -> {tgt_id}")

    def targets_of(self, rel_type: str, src_id: str) -> dict[str, set[str]]:
        """Targets of one node's relationships, grouped by target label."""
        targets: dict[str, set[str]] = defaultdict(set)
        for (_src, rel, tgt_label, src, tgt) in self.edges:
            if rel == rel_type and src == src_id:
                targets[tgt_label].add(tgt)
        return dict(targets)

    def snapshot(self):
        """Everything a re-publish must leave untouched.

        Deep-copied on purpose: node and relationship property dicts are
        mutated in place by the upserts, so a shallow copy would compare
        equal to itself no matter what a second load wrote.
        """
        return (
            deepcopy(dict(self.nodes)),
            dict(self.person_labels),
            deepcopy(self.edges),
        )
