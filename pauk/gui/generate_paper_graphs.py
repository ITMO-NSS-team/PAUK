"""Builds web/paper-graphs.js - one paper as a graph, for the map's profiles.

Reads whatever OSA exports are on disk (see `pauk/paper_graph/`) and writes
the publication -> sections -> claims -> files structure the publication
profile draws. Reads no database: the graph of a paper is per-paper data
that never entered Neo4j.

Usage:
  uv run python -m pauk.gui.generate_paper_graphs [--src data/paper_graphs]
                                                  [--out-dir DIR] [--public]
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from pauk.models import PaperGraph
from pauk.paper_graph import load_paper_graphs

logger = logging.getLogger(__name__)

OUT_FILE = "paper-graphs.js"


def _claim_payload(claim) -> dict:
    """One claim, without the fields it has nothing in.

    Every paper graph ships with the map as one file, so an absent value is
    left out rather than written as null - the frontend reads them the same
    way either way.
    """
    payload = {"id": claim.claim_id, "claim": claim.claim, "section": claim.section_id}
    optional = {
        "evidence": claim.original_text,
        "category": claim.category,
        "verifiability": claim.verifiability,
        "value": claim.value,
        "correspondence": claim.correspondence_percent,
        "reasoning": claim.reasoning,
    }
    payload.update({k: v for k, v in optional.items() if v not in (None, "")})
    if claim.contradiction:
        payload["contradiction"] = True
    if claim.missing:
        payload["missing"] = claim.missing
    payload["files"] = [
        {"path": f.path, **({"url": f.url} if f.url else {})} for f in claim.files
    ]
    payload["sources"] = [
        {"id": s.source_id, "kind": s.kind,
         **{k: v for k, v in (("text", s.text), ("page", s.page), ("label", s.label))
            if v not in (None, "")}}
        for s in claim.sources
    ]
    return payload


def _document_payload(document) -> dict:
    """Elements keyed by id, edges as rows - the shape the profile walks."""
    return {
        "nodes": {
            n.node_id: {k: v for k, v in (("name", n.name), ("page", n.page), ("text", n.text))
                        if v not in (None, "")}
            for n in document.nodes
        },
        "edges": [
            [e.source, e.target, e.edge_type, *([e.label] if e.label else [])]
            for e in document.edges
        ],
    }


def build_paper_graphs(graphs: list[PaperGraph]) -> dict[str, dict]:
    """Publication id -> the payload its profile draws."""
    out = {}
    for graph in graphs:
        out[graph.publication_id] = {
            "title": graph.title or "",
            "repo_url": graph.repository_url or "",
            "source": graph.source or "",
            "sections": [
                {"id": s.section_id, "name": s.name, "level": s.level,
                 **({"numbering": s.numbering} if s.numbering else {})}
                for s in graph.sections
            ],
            "claims": [_claim_payload(c) for c in graph.claims],
        }
        if graph.document and graph.document.edges:
            out[graph.publication_id]["doc"] = _document_payload(graph.document)
    return out


def write_paper_graphs(src_dir: Path, out_dir: Path) -> dict[str, int]:
    """Read every export under `src_dir` and write paper-graphs.js.

    Always writes the file, even with nothing to put in it: the page loads
    it as a plain script, and an empty map beats a 404 in the console.
    """
    graphs = load_paper_graphs(Path(src_dir))
    payload = build_paper_graphs(graphs)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / OUT_FILE
    path.write_text(
        "window.PAPER_GRAPHS=" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + ";if(typeof window._onPaperGraphsReady==='function')window._onPaperGraphsReady();\n",
        encoding="utf-8",
    )
    claims = sum(len(g.claims) for g in graphs)
    files = sum(len(c.files) for g in graphs for c in g.claims)
    sources = sum(len(c.sources) for g in graphs for c in g.claims)
    logger.info(
        "Wrote %s: %d paper(s), %d claim(s), %d file link(s), %d source(s)",
        path, len(graphs), claims, files, sources,
    )
    return {"paper_graphs": len(graphs), "paper_claims": claims, "paper_claim_files": files,
            "paper_claim_sources": sources}


def main():
    from pauk.settings import settings

    parser = argparse.ArgumentParser(description="Paper graphs for the map's publication profiles")
    parser.add_argument(
        "--src",
        type=Path,
        default=settings.paper_graphs_dir,
        help=f"directory of OSA exports (default: {settings.paper_graphs_dir})",
    )
    parser.add_argument("--public", action="store_true", help="write into the public build")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help=f"where {OUT_FILE} goes (default: the map directory of the chosen build)",
    )
    args = parser.parse_args()
    if args.out_dir is None:
        args.out_dir = settings.map_out_dir(args.public)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    write_paper_graphs(args.src, args.out_dir)


if __name__ == "__main__":
    main()
