"""Preview paper graphs on the map without Neo4j.

The map only draws a paper graph inside a publication's profile, and a
profile only exists for a publication in the graph snapshot - which comes
from Neo4j. For papers an OSA run has been through but PAUK has not, this
stages the run as an export (`docs/architecture/paper-graph.md`) and writes
a map build whose snapshot holds just the staged papers, under one stand-in
ITMO author: the map drops publications with no ITMO author.

A run of `mass_launch_osa_tool.py` is split over two directories, neither
in the layout the loader reads:

  <dataset>/paper_analysis/<id>/   run.json, document_claims/,
                                   claim_verification/report.json
  <dataset>/processed/<id>/        graph.json (the document graph)

Staging is cumulative: every export under `data/paper_graphs/` with a
meta.json lands on the preview map, so adding a paper keeps the others.

Usage:
    uv run python scripts/preview_paper_graphs.py --dataset DIR [ID ...]
        [--map-dir data/preview_map]
    PAUK_MAP_DIR=data/preview_map uv run python -m pauk.gui.serve 8502

Without IDs, every finished run under <dataset>/paper_analysis is staged.
Writes data/paper_graphs/<id>/ and the map directory; reads no database.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
from pathlib import Path

from pauk.cache.graph_snapshot import write_snapshot
from pauk.gui.generate_data import write_graph_files
from pauk.gui.generate_paper_graphs import write_paper_graphs
from pauk.settings import settings

logger = logging.getLogger(__name__)

PERSON, DEPT = "preview-person", "preview-dept"


def stage(dataset: Path, pid: str, dest_root: Path) -> Path:
    """One OSA run as an export directory the paper-graph loader reads."""
    analysis, processed = dataset / "paper_analysis" / pid, dataset / "processed" / pid
    run = json.loads((analysis / "run.json").read_text(encoding="utf-8"))
    out = dest_root / pid
    out.mkdir(parents=True, exist_ok=True)

    meta = {"publication_id": pid, "title": run.get("publication_title"),
            "repository_url": run.get("repo_url"),
            # Not read by the loader; the preview snapshot dates the paper by it.
            "publication_date": run.get("publication_date")}
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    shutil.copy(analysis / "document_claims" / "claims.json", out / "claims.json")
    shutil.copy(analysis / "document_claims" / "sections.json", out / "sections.json")

    graph_path = processed / "graph.json"
    if graph_path.is_file():
        shutil.copy(graph_path, out / "graph.json")
        # processed/layout.json carries no element text; the graph's nodes
        # do, and the loader looks for the text in the layout.
        nodes = json.loads(graph_path.read_text(encoding="utf-8")).get("nodes", [])
        (out / "elements.json").write_text(json.dumps({"elements": nodes}, ensure_ascii=False),
                                           encoding="utf-8")

    # Verification names one evidence file per implemented claim; as an
    # experiment keyed by the claim's own text, the loader folds it back in.
    report = analysis / "claim_verification" / "report.json"
    rows = json.loads(report.read_text(encoding="utf-8"))["result"]["claims"] if report.is_file() else []
    experiments = []
    for row in rows:
        impl = row.get("implementation") or {}
        implemented = bool(impl.get("implemented"))
        experiments.append({
            "description_from_paper": row["claim"],
            "impl_src_path": [impl["evidence_file"]] if implemented and impl.get("evidence_file") else [],
            "correspondence_percent": 100 if implemented else 0,
            "reasoning": " ".join(x for x in (
                f"[{impl['confidence']}]" if impl.get("confidence") else "", impl.get("explanation") or ""
            ) if x),
        })
    (out / "experiments.json").write_text(json.dumps({"experiments": experiments}, ensure_ascii=False,
                                                     indent=1), encoding="utf-8")
    logger.info("staged %s: %d verified claim(s)", out, len(experiments))
    return out


def preview_snapshot(exports: Path) -> dict[str, list]:
    """A snapshot of every staged paper, in the shape `load_db()` returns."""
    graph = {
        "persons": [{"id": PERSON, "first_name_ru": None, "second_name_ru": None, "surname_ru": None,
                     "first_name_en": "Preview", "second_name_en": None, "surname_en": "Author",
                     "name_ru": None, "name_variants": [], "degree": None, "github": None,
                     "orcid": None}],
        "departments": [{"id": DEPT, "name_ru": "Предпросмотр", "name_en": "Preview"}],
        "person_depts": [[PERSON, DEPT]],
        "publications": [], "repositories": [], "authorship": [], "pub_depts": [],
        "repo_pubs": [], "repo_persons": [], "repo_depts": [],
    }
    for meta_path in sorted(exports.glob("*/meta.json")):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        pid = meta.get("publication_id") or meta_path.parent.name
        repo = meta.get("repository_url") or meta.get("repo_url")
        date = meta.get("publication_date") or ""
        graph["publications"].append([pid, meta.get("title") or pid, None, None, date or None,
                                      int(date[:4]) if date[:4].isdigit() else None, bool(repo), repo])
        graph["authorship"].append([pid, PERSON])
        graph["pub_depts"].append([pid, DEPT])
        if repo:
            rid = f"repo-{pid}"
            parts = repo.rstrip("/").split("/")
            graph["repositories"].append([rid, parts[-1], repo, None, 0, parts[-2]])
            graph["repo_pubs"].append([rid, pid])
            graph["repo_depts"].append([rid, DEPT])
    return graph


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("ids", nargs="*", help="OpenAlex work ids (default: every finished run)")
    parser.add_argument("--dataset", type=Path, required=True,
                        help="directory holding paper_analysis/ and processed/")
    parser.add_argument("--map-dir", type=Path, default=Path("data/preview_map"),
                        help="map build to write; serve it with PAUK_MAP_DIR=<this>")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")

    ids = args.ids
    if not ids:
        ids = sorted(
            p.parent.name for p in (args.dataset / "paper_analysis").glob("*/run.json")
            if json.loads(p.read_text(encoding="utf-8")).get("status") == "ok"
        )
    exports = settings.paper_graphs_dir
    for pid in ids:
        stage(args.dataset, pid, exports)

    # serve.py reads <PAUK_MAP_DIR>/private, so the build goes there.
    out_dir = args.map_dir / "private"
    out_dir.mkdir(parents=True, exist_ok=True)
    snapshot = args.map_dir / "snapshot.json"
    write_snapshot(snapshot, preview_snapshot(exports))
    write_graph_files(snapshot, out_dir)
    write_paper_graphs(exports, out_dir)
    print(f"\nServe it:\n  PAUK_MAP_DIR={args.map_dir.resolve()} uv run python -m pauk.gui.serve 8502")


if __name__ == "__main__":
    main()
