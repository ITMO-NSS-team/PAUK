"""Cache snapshot -> layout -> JSON for the site.

The stage logic lives in `authorship.py`, `departments.py`, `layout.py`,
`nodes.py` and `edges.py`; this file wires them together and writes the files.

- Nodes come in two forms: "summary" (what the map needs to draw a point, in
  `graph-data.json`) and "detail" (extended fields `pauk/gui/web` loads lazily).
  Departments have no detail file.
- Output is plain JSON, read by `core/data.ts::loadGraphData()`.
- Two variants from one build: `private/` (lab server, full names and personal
  fields) and `public/` (GitHub Pages), which `public.py::to_public` rewrites
  from the private one: masked names, renumbered author keys, whitelisted
  fields only. The site picks one by build mode (`vite.config.ts`).
"""

from __future__ import annotations

import json
import logging
import math
import time
from hashlib import sha256
from pathlib import Path

from pauk.cache import read_snapshot
from pauk.storage import AtomicWriter

from .authorship import build_authorship_index
from .departments import DepartmentAssigner, repo_groups
from .edges import EdgeBuilder
from .layout import GraphLayoutBuilder
from .nodes import AuthorNodeBuilder, PubNodeBuilder, RepoNodeBuilder
from .public import to_public

logger = logging.getLogger(__name__)


class GraphDataBuilder:
    """Builds the whole graph (layout, nodes, edges) from a `pauk.cache` snapshot.

    `build()` is called exactly once per run.
    """

    def __init__(self, db: dict[str, list[dict]], seed: int,
                 similarity: dict[tuple[str, str], float] | None = None) -> None:
        """Stores the input; the actual build only happens in `build()`.

        Args:
            db: Graph snapshot in the shape `pauk.cache.export::load_db()` returns.
            seed: ForceAtlas2 seed (for layout reproducibility).
        """
        self.db = db
        self.seed = seed
        self.similarity = similarity

    def build(self) -> tuple[dict, dict[str, list[dict]]]:
        """Builds the whole graph from the snapshot.

        Returns:
            `(summary, detail)`:
            - `summary` - what's written to `graph-data.json` (department
              table, all edges, summary node rows);
            - `detail` - a dict `{"authors": [...], "repos": [...], "pubs": [...]}`,
              each list written to its own `*-detail.json`.
        """
        db = self.db
        # dept_name falls back to the English name only so a department "exists"
        # (a filter in DepartmentAssigner.assign(), not for display);
        # dept_name_en is purely English and goes into the final table.
        dept_name = {row["id"]: (row["name_ru"] or row["name_en"] or "") for row in db["departments"]}
        dept_name_en = {row["id"]: (row["name_en"] or "") for row in db["departments"]}

        # Stage order matters: assign() needs authorship, and the assignment feeds
        # build_table(), layout and the node/edge builders below.
        authorship = build_authorship_index(db)
        # The snapshot holds every external person and all their authorship;
        # only those with an ITMO coauthored publication make it into the graph.
        db = {
            **db,
            "persons": [
                row for row in db["persons"] if row["id"] not in authorship.external_ids or row["id"] in authorship.author_pubs
            ],
            "authorship": [row for row in db["authorship"] if row["pid"] in authorship.pub_ids],
        }
        assigner = DepartmentAssigner(db, authorship)
        assignment = assigner.assign(dept_name)
        table = assigner.build_table(dept_name, dept_name_en, assignment)
        groups = repo_groups(db, assignment, table)
        layout = GraphLayoutBuilder(db, authorship, assignment, self.similarity).build(self.seed)

        # The three node kinds are built independently but all need table
        # (dept/color) and their own slice of layout (positions).
        authors_summary, authors_detail = AuthorNodeBuilder(
            db, authorship, assignment, table, layout.pos_authors
        ).build()
        repos_summary, repos_detail = RepoNodeBuilder(db, assignment, table, groups.group_of, layout.pos_repos).build()
        pubs_summary, pubs_detail = PubNodeBuilder(authorship, assignment, table, layout.pos_pubs).build()
        edges = EdgeBuilder(db, authorship, assignment, table, layout).build()

        # **edges puts the seven edge keys at the top level of summary, which is
        # how pauk/gui/web reads them (no nested "edges" object).
        summary = {
            "departments": table.departments,
            "repo_groups": groups.groups,
            "authors": authors_summary,
            "repos": repos_summary,
            "pubs": pubs_summary,
            **edges,
        }
        detail = {"authors": authors_detail, "repos": repos_detail, "pubs": pubs_detail}
        return summary, detail


def dump_json(data, path: Path) -> None:
    """Writes data as plain JSON, atomically.

    The web server may be serving these files during a rebuild and must never
    see a half-written one.
    """
    with AtomicWriter(path) as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
    logger.info("Wrote %s (%.1f MB)", path, path.stat().st_size / 1e6)


def _similar_repositories(snapshot_digest: str, db: dict, path: Path) -> dict[str, list[dict]]:
    """Validate derived candidates against the exact snapshot before publishing."""
    data = json.loads(path.read_bytes())
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("unsupported similarity result")
    if data.get("snapshot_sha256") != snapshot_digest:
        raise ValueError("similarity result belongs to a different snapshot")
    pairs = data.get("pairs")
    if not isinstance(pairs, list):
        raise ValueError("similarity result is missing pairs")
    ids = {row["id"] for row in db["repositories"]}
    neighbors: dict[str, list[dict]] = {}
    seen = set()
    for pair in pairs:
        if not isinstance(pair, dict):
            raise ValueError("invalid similarity pair")
        a, b, score = pair.get("a"), pair.get("b"), pair.get("score")
        if not isinstance(a, str) or not isinstance(b, str) or a not in ids or b not in ids or a >= b:
            raise ValueError("similarity pair has invalid endpoints")
        if (a, b) in seen:
            raise ValueError("duplicate similarity pair")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not -1 <= score <= 1:
            raise ValueError("invalid similarity score")
        seen.add((a, b))
        neighbors.setdefault(a, []).append({"key": b, "score": score})
        neighbors.setdefault(b, []).append({"key": a, "score": score})
    for items in neighbors.values():
        items.sort(key=lambda row: (-row["score"], row["key"]))
    return neighbors


def write_site_data(snapshot: Path, out_dir: Path, seed: int, *, similarity: Path | None = None,
                    similarity_layout: bool = False) -> dict[str, int]:
    """Builds the site data from a snapshot and writes it into `out_dir`; what `pauk gui build` runs.

    Args:
        snapshot: Graph snapshot taken by `pauk cache export`.
        out_dir: Base folder - `public/` and `private/` live inside it.
        seed: ForceAtlas2 layout seed.

    Returns:
        What went onto the map - `map_authors`/`map_pubs`/`map_repos`/
        `map_departments`/`map_edges` (the admin panel shows these after a rebuild).
    """
    t0 = time.time()
    if similarity_layout and not similarity:
        raise ValueError("--similarity-layout requires --similarity")
    if similarity:
        raw_snapshot = snapshot.read_bytes()
        db = json.loads(raw_snapshot)
        if not isinstance(db, dict):
            raise ValueError("graph snapshot is not a JSON object")
        similar = _similar_repositories(sha256(raw_snapshot).hexdigest(), db, similarity)
        del raw_snapshot
    else:
        db = read_snapshot(snapshot)
        similar = {}
    layout_pairs = {(a, row["key"]): row["score"] for a, items in similar.items() for row in items
                    if a < row["key"]} if similarity_layout else {}
    if any(score <= 0 for score in layout_pairs.values()):
        raise ValueError("similarity layout requires positive scores")
    summary, detail = GraphDataBuilder(db, seed=seed, similarity=layout_pairs).build()
    for row in detail["repos"]:
        if row["key"] in similar:
            row["similar_repos"] = similar[row["key"]]
    public_summary, public_detail = to_public(summary, detail, db["persons"])

    public_dir = out_dir / "public"
    private_dir = out_dir / "private"
    public_dir.mkdir(parents=True, exist_ok=True)
    private_dir.mkdir(parents=True, exist_ok=True)
    for folder, (graph, details) in ((private_dir, (summary, detail)), (public_dir, (public_summary, public_detail))):
        dump_json(graph, folder / "graph-data.json")
        for kind, rows in details.items():
            dump_json(rows, folder / f"{kind}-detail.json")

    logger.info("Done in %.1f s", time.time() - t0)
    return {
        "map_authors": len(summary["authors"]),
        "map_pubs": len(summary["pubs"]),
        "map_repos": len(summary["repos"]),
        "map_departments": len(summary["departments"]),
        "map_edges": len(summary["all_edges"]),
    }
