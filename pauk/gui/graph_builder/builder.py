"""Cache snapshot -> layout -> JSON for the site - entry point and
orchestration. The actual stage logic lives in separate modules
(`authorship.py`, `departments.py`, `layout.py`, `nodes.py`, `edges.py`) -
this file only wires them together in the right order and handles the
CLI/disk writes.

- `db` is in the shape `pauk.cache.export::load_db()` returns: lists of
  dicts (`cypher_dict` for persons/publications/repositories), not a mix of
  dicts and positional tuples like the old GUI's `generate_data.py`.
- Nodes are built in two forms at once - "summary" (what's needed to draw a
  point on the map: `key`/`kind`/`dept`/`label`/`rank`/`gx`/`gy` plus one
  summary number) and "detail" (everything else - extended fields `pauk/gui/web`
  lazily loads after the map). This used to exist for exactly one entity
  (publications - a separate `build_search_detail()`/`graph-search.js`);
  here it's generalized to all four types instead of reinvented.
- Departments have no separate detail file yet - today `departments` has no
  field that isn't already in summary.
- Output is plain JSON (not `window.GRAPH=...;`): `pauk/gui/web` already reads
  plain JSON (`core/data.ts::loadGraphData()`), the wrapper was only ever
  needed by the old GUI, unrelated here.
- One run, no `--public`/`--private` mode: generation used to run twice (once
  per build variant), producing an almost identical `graph-data.json` that
  only differed in whether the author label was truncated. `GraphDataBuilder`
  now computes exactly one version of everything - the map label is currently
  the full form (`author_label(..., public=False)`, see `nodes.py` -
  readability won out over anonymizing a label next to a file already shared
  across build variants; flip back to `public=True` there for an actual
  public deploy), and `authors-detail.json` always holds every person field
  (private ones included), untrimmed. Public/private is decided not by content but by disk
  location: `write_site_data()` writes `graph-data.json`/`repos-detail.json`/
  `pubs-detail.json` into `public/` (no personal field lives there), and
  `authors-detail.json` only into `private/`. For local development (today's
  only consumer, `pauk/gui/web`, serves static files from ONE folder -
  `vite.config.ts::publicDir`) `graph-data.json`/`repos-detail.json`/
  `pubs-detail.json` are ADDITIONALLY written into `private/` too - so all
  four files end up there at once, same as before, no changes needed in
  `pauk/gui/web`. The actual split of "what leaves the corporate network" happens
  at deploy time - only `public/` is taken from there, `private/` never
  reaches the public artifact.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from .authorship import build_authorship_index
from .departments import DepartmentAssigner
from .edges import EdgeBuilder
from .layout import GraphLayoutBuilder
from .nodes import AuthorNodeBuilder, PubNodeBuilder, RepoNodeBuilder

logger = logging.getLogger(__name__)


class GraphDataBuilder:
    """Builds the whole graph: layout + nodes + edges, from a
    `pauk.cache` snapshot - holds `db`/`seed` as state, `build()` is
    called exactly once per run.
    """

    def __init__(self, db: dict[str, list[dict]], seed: int) -> None:
        """Stores the input - the actual build only happens in `build()`.

        Args:
            db: Graph snapshot in the shape `pauk.cache.export::load_db()` returns.
            seed: ForceAtlas2 seed (for layout reproducibility).
        """
        self.db = db
        self.seed = seed

    def build(self) -> tuple[dict, dict[str, list[dict]]]:
        """Builds the whole graph: layout + nodes + edges, from the snapshot.

        Returns:
            `(summary, detail)`:
            - `summary` - what's written to `graph-data.json` (department
              table, all edges, summary node rows);
            - `detail` - a dict `{"authors": [...], "repos": [...], "pubs": [...]}`,
              each list written to its own `*-detail.json`.
        """
        db = self.db
        # dept_name - the Russian name OR the English one as a fallback
        # (only needed so a department "exists" at all - a filter in
        # DepartmentAssigner.assign(), not for display); dept_name_en -
        # separate, purely English, goes straight into the final table.
        dept_name = {row["id"]: (row["name_ru"] or row["name_en"] or "") for row in db["departments"]}
        dept_name_en = {row["id"]: (row["name_en"] or "") for row in db["departments"]}

        # Stage order matters: authorship is needed for assign() (who
        # authored what), assignment feeds both build_table() (who belongs
        # to which department) and layout (department edges) and node/edge building below.
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
        layout = GraphLayoutBuilder(db, authorship, assignment).build(self.seed)

        # The three node kinds are built independently (their own Builder
        # each), but all need table (for dept/color) and their own slice of
        # layout (positions).
        authors_summary, authors_detail = AuthorNodeBuilder(
            db, authorship, assignment, table, layout.pos_authors
        ).build()
        repos_summary, repos_detail = RepoNodeBuilder(db, assignment, table, layout.pos_repos).build()
        pubs_summary, pubs_detail = PubNodeBuilder(authorship, assignment, table, layout.pos_pubs).build()
        edges = EdgeBuilder(db, authorship, assignment, table, layout).build()

        # **edges unpacks all seven edge keys (coauth_edges/pub_edges/...)
        # directly into the top level of summary - that's how pauk/gui/web sees
        # them, no nested "edges" object in the JSON.
        summary = {
            "departments": table.departments,
            "authors": authors_summary,
            "repos": repos_summary,
            "pubs": pubs_summary,
            **edges,
        }
        detail = {"authors": authors_detail, "repos": repos_detail, "pubs": pubs_detail}
        return summary, detail


def dump_json(data, path: Path) -> None:
    """Writes data as plain JSON (not `window.X=...;` - that wrapper was
    only ever needed by the old GUI, `pauk/gui/web` reads JSON directly via `fetch`)."""
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    logger.info("Wrote %s (%.1f MB)", path, path.stat().st_size / 1e6)


def write_site_data(snapshot: Path, out_dir: Path, seed: int) -> dict[str, int]:
    """Builds the site data from a snapshot and writes it into `out_dir` -
    what `pauk gui build` runs.

    Args:
        snapshot: Graph snapshot taken by `pauk cache export`.
        out_dir: Base folder - `public/` and `private/` live inside it.
        seed: ForceAtlas2 layout seed.

    Returns:
        What went onto the map - `map_authors`/`map_pubs`/`map_repos`/
        `map_departments`/`map_edges` (the admin panel shows these after a rebuild).
    """
    from pauk.cache.graph_snapshot import read_snapshot

    public_dir = out_dir / "public"
    private_dir = out_dir / "private"
    public_dir.mkdir(parents=True, exist_ok=True)  # exist_ok - a second run into the same folder shouldn't fail
    private_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    summary, detail = GraphDataBuilder(read_snapshot(snapshot), seed=seed).build()

    # graph-data.json and the detail files with no personal fields go into
    # public (safe to deploy externally), and are ADDITIONALLY duplicated
    # into private - the only reason is that pauk/gui/web today serves static
    # files from ONE folder (vite.config.ts::publicDir), and Vite can't
    # take two publicDirs at once. authors-detail.json goes only into
    # private, nowhere else - it holds the only truly personal fields
    # (email/google_scholar/affiliations/...).
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
