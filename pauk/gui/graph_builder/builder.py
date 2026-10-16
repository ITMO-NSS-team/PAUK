"""Cache snapshot -> layout -> JSON for the site.

The stage logic lives in `authorship.py`, `departments.py`, `layout.py`,
`nodes.py` and `edges.py`; this file wires them together and writes the files.

- Nodes come in two forms: "summary" (what the map needs to draw a point, in
  `graph-data.json`) and "detail" (extended fields `pauk/gui/web` loads lazily).
  Departments have no detail file.
- Output is plain JSON, read by `core/data.ts::loadGraphData()`.
- Public/private is decided by disk location, not by trimming content:
  `authors-detail.json`, the only file with personal fields, goes only into
  `private/`. The map label is the full form (`author_label(..., public=False)`,
  see `nodes.py`); switch to `public=True` there for anonymized labels.
- `graph-data.json` and the non-author detail files are also written into
  `private/`, because Vite serves ONE folder (`vite.config.ts::publicDir`) and
  local development needs all four files there. Deploy takes only `public/`.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from pauk.cache import read_snapshot
from pauk.storage import AtomicWriter

from .authorship import build_authorship_index
from .departments import DepartmentAssigner, repo_groups
from .edges import EdgeBuilder
from .layout import GraphLayoutBuilder
from .nodes import AuthorNodeBuilder, PubNodeBuilder, RepoNodeBuilder

logger = logging.getLogger(__name__)


class GraphDataBuilder:
    """Builds the whole graph (layout, nodes, edges) from a `pauk.cache` snapshot.

    `build()` is called exactly once per run.
    """

    def __init__(self, db: dict[str, list[dict]], seed: int) -> None:
        """Stores the input; the actual build only happens in `build()`.

        Args:
            db: Graph snapshot in the shape `pauk.cache.export::load_db()` returns.
            seed: ForceAtlas2 seed (for layout reproducibility).
        """
        self.db = db
        self.seed = seed

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
        layout = GraphLayoutBuilder(db, authorship, assignment).build(self.seed)

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


def write_site_data(snapshot: Path, out_dir: Path, seed: int) -> dict[str, int]:
    """Builds the site data from a snapshot and writes it into `out_dir`; what `pauk gui build` runs.

    Args:
        snapshot: Graph snapshot taken by `pauk cache export`.
        out_dir: Base folder - `public/` and `private/` live inside it.
        seed: ForceAtlas2 layout seed.

    Returns:
        What went onto the map - `map_authors`/`map_pubs`/`map_repos`/
        `map_departments`/`map_edges` (the admin panel shows these after a rebuild).
    """
    public_dir = out_dir / "public"
    private_dir = out_dir / "private"
    public_dir.mkdir(parents=True, exist_ok=True)
    private_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    summary, detail = GraphDataBuilder(read_snapshot(snapshot), seed=seed).build()

    # graph-data.json and the detail files without personal fields go into
    # public (safe to deploy) and are duplicated into private, because
    # pauk/gui/web serves static files from ONE folder
    # (vite.config.ts::publicDir). authors-detail.json holds the only truly
    # personal fields (email/google_scholar/affiliations/...) and goes only
    # into private.
    dump_json(summary, public_dir / "graph-data.json")
    dump_json(summary, private_dir / "graph-data.json")
    for kind, rows in detail.items():
        dump_json(rows, private_dir / f"{kind}-detail.json")
        if kind != "authors":
            dump_json(rows, public_dir / f"{kind}-detail.json")

    logger.info("Done in %.1f s", time.time() - t0)
    return {
        "map_authors": len(summary["authors"]),
        "map_pubs": len(summary["pubs"]),
        "map_repos": len(summary["repos"]),
        "map_departments": len(summary["departments"]),
        "map_edges": len(summary["all_edges"]),
    }
