"""Mock external clients backed by the synthetic universe, plus an
in-memory Neo4j stand-in that records what the loader would upsert."""

from __future__ import annotations

from collections import defaultdict

import requests


def _http_404(url: str) -> requests.HTTPError:
    return requests.HTTPError(f"404 Client Error: Not Found for url: {url}")


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
