"""Builds web/graph-data.js and web/graph-search.js from a graph snapshot.

Usage:
  uv run python -m pauk.cli cache export  # writes the snapshot, once
  uv run python -m pauk.gui.generate_data [--out-dir web] [--seed 42] [--cache path]
"""

import argparse
import json
import logging
import random
import time
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

import networkx as nx

from .config import (
    COAUTH_MIN_W,
    FA2_ITER_AUTHORS,
    FA2_ITER_PUBS,
    FA2_ITER_REPOS,
    MIN_SEP_AUTHORS,
    MIN_SEP_PUBS,
    NO_DEPT_COLOR,
    NO_DEPT_NAME,
    NO_DEPT_NAME_EN,
    PUB_DEPT_EDGE_K,
    PUB_DEPT_EDGE_WEIGHT,
    PUB_EDGE_MIN_W,
    PUB_LAYOUT_TOP_K,
)
from .layout import (
    dense_rank,
    fa2_blended_layout,
    fit_coords,
    golden_color,
    majority_dept,
    sparse_dept_edges,
    spread_min_distance,
)

logger = logging.getLogger(__name__)


# TODO: delete function, add new methond
def split_full_name(full_name) -> tuple[str, str, str]:
    """Split a written-out name into (surname, given, patronymic)"""
    text = " ".join((full_name or "").split())
    if not text:
        return "", "", ""
    if "," in text:
        surname, _, rest = text.partition(",")
        parts = rest.split()
    else:
        words = text.split()
        surname, parts = words[-1], words[:-1]
    parts = [part for part in parts if part[:1].isupper()]
    return surname.strip(), (parts[0] if parts else ""), (parts[1] if len(parts) > 1 else "")


def author_label(
    surname_ru, first_name_ru, second_name_ru, name_en, name_ru=None, public: bool = False
) -> str:
    """One shape for every author: surname first, then initials"""
    surname, given, patronymic = surname_ru or "", first_name_ru or "", second_name_ru or ""
    if not surname:
        surname, given, patronymic = split_full_name(name_ru or name_en)
    if not surname:
        return name_ru or name_en or ""
    if public and len(surname) > 3:
        surname = surname[:3] + ".."
    if given and patronymic:
        return f"{surname} {given[0].upper()}.{patronymic[0].upper()}."
    if patronymic:  # only the patronymic survived — treat it as the initial
        return f"{surname} {patronymic[0].upper()}."
    if given:
        return f"{surname} {given[0].upper()}." if public else f"{surname} {given}"
    return surname


def author_variants(row) -> list[str]:
    """Other spellings of this person's name, without the ones on show.

    The card already displays the label, the romanized name and the full
    Russian name; everything else OpenAlex knows about this author (and
    the full Russian name when the label is only surname + initials) goes
    into the collapsed list.
    """
    shown = {
        (row.get("name_en") or "").strip().casefold(),
        author_label(
            row["surname_ru"],
            row["first_name_ru"],
            row["second_name_ru"],
            row["name_en"],
            row.get("name_ru"),
        ).casefold(),
    }
    candidates = [row.get("name_ru") or "", *(row.get("name_variants") or [])]
    variants = []
    for value in candidates:
        cleaned = " ".join((value or "").split())
        if cleaned and cleaned.casefold() not in shown:
            shown.add(cleaned.casefold())
            variants.append(cleaned)
    return variants


def build_graph_data(db, seed: int, public: bool = False):
    dept_name = {row["id"]: (row["name_ru"] or row["name_en"] or "") for row in db["departments"]}
    dept_name_en = {row["id"]: (row["name_en"] or "") for row in db["departments"]}

    # --- authorship: only publications with at least one ITMO author ----------
    pub_authors = defaultdict(list)
    author_pubs = defaultdict(list)
    for pid, per in db["authorship"]:
        pub_authors[pid].append(per)
        author_pubs[per].append(pid)

    pubs_rows = [r for r in db["publications"] if r[0] in pub_authors]
    pub_ids = {r[0] for r in pubs_rows}
    logger.info(
        "Publications with ITMO authors: %d of %d",
        len(pubs_rows),
        len(db["publications"]),
    )

    # --- static author departments (:Person:Itmo)-[:BELONGS_TO]->(:Department) -
    static_depts = {row["id"]: [] for row in db["persons"]}
    for per, did in db["person_depts"]:
        if did in dept_name:
            static_depts[per].append(did)

    # --- publication departments: full list + primary --------------------------
    pub_dept_rows = defaultdict(list)
    for pid, did in db["pub_depts"]:
        if pid in pub_ids and did in dept_name and did not in pub_dept_rows[pid]:
            pub_dept_rows[pid].append(did)

    pub_primary = {}
    for pid in pub_ids:
        primary = majority_dept(static_depts.get(per, []) for per in pub_authors[pid])
        if primary is None and pub_dept_rows.get(pid):
            primary = pub_dept_rows[pid][0]
        pub_primary[pid] = primary

    # --- author department: from their most recent publication ------------------
    pub_date = {r[0]: (r[4] or "") for r in pubs_rows}
    author_dept = {}
    for per in static_depts:
        dept = None
        for pid in sorted(
            author_pubs.get(per, []),
            key=lambda p: (pub_date.get(p, ""), p),
            reverse=True,
        ):
            if pub_primary.get(pid):
                dept = pub_primary[pid]
                break
        author_dept[per] = dept

    # --- repository department --------------------------------------------------
    repo_pub_map = defaultdict(list)
    for rid, pid in db["repo_pubs"]:
        if pid in pub_ids:
            repo_pub_map[rid].append(pid)
    repo_dept_rows = defaultdict(list)
    for rid, did in db["repo_depts"]:
        if did in dept_name and did not in repo_dept_rows[rid]:
            repo_dept_rows[rid].append(did)

    repo_dept = {}
    for rid, _, _, _, _, _ in db["repositories"]:
        primary = majority_dept(
            [pub_primary[p]] for p in repo_pub_map.get(rid, []) if pub_primary.get(p)
        )
        if primary is None and repo_dept_rows.get(rid):
            primary = repo_dept_rows[rid][0]
        repo_dept[rid] = primary

    # --- graph department table: sort by size, reindex --------------------------
    usage = Counter()
    for d in author_dept.values():
        if d:
            usage[d] += 1
    for d in pub_primary.values():
        if d:
            usage[d] += 1
    for d in repo_dept.values():
        if d:
            usage[d] += 1
    for rows in (pub_dept_rows, repo_dept_rows):
        for depts in rows.values():
            for d in depts:
                usage[d] += 0

    ordered = sorted(usage, key=lambda d: (-usage[d], dept_name[d]))
    gid = {d: i for i, d in enumerate(ordered)}
    no_dept_gid = len(ordered)

    def g(dept_db_id):
        return gid[dept_db_id] if dept_db_id else no_dept_gid

    n_auth = Counter(g(d) for d in author_dept.values())
    n_pub = Counter(g(pub_primary[p]) for p in pub_ids)
    n_repo = Counter(g(repo_dept[r[0]]) for r in db["repositories"])

    departments = [
        {
            "id": gid[d],
            "name": dept_name[d],
            "name_en": dept_name_en[d],
            "color": golden_color(gid[d]),
            "n": n_auth[gid[d]] + n_pub[gid[d]] + n_repo[gid[d]],
            "n_authors": n_auth[gid[d]],
            "n_pubs": n_pub[gid[d]],
            "n_repos": n_repo[gid[d]],
        }
        for d in ordered
    ]
    departments.append(
        {
            "id": no_dept_gid,
            "name": NO_DEPT_NAME,
            "name_en": NO_DEPT_NAME_EN,
            "color": NO_DEPT_COLOR,
            "n": n_auth[no_dept_gid] + n_pub[no_dept_gid] + n_repo[no_dept_gid],
            "n_authors": n_auth[no_dept_gid],
            "n_pubs": n_pub[no_dept_gid],
            "n_repos": n_repo[no_dept_gid],
        }
    )
    logger.info('Departments: %d (+ "%s")', len(ordered), NO_DEPT_NAME)

    # --- co-authorship graph and FA2 layout -------------------------------------
    # layout weight = joint publications + joint repos + shared dept; exported
    coauth = Counter()
    for _pid, pers in pub_authors.items():
        for a, b in combinations(sorted(set(pers)), 2):
            coauth[(a, b)] += 1

    rng = random.Random(seed)
    author_layout_w = Counter(coauth)
    repo_contributors = defaultdict(set)
    for rid, per, _role in db["repo_persons"]:
        repo_contributors[rid].add(per)
    for pers in repo_contributors.values():
        for a, b in combinations(sorted(pers), 2):
            author_layout_w[(a, b)] += 1
    for pair, w in sparse_dept_edges(set(static_depts), author_dept, rng).items():
        author_layout_w[pair] += w

    t0 = time.time()
    pos_authors, (n_giant, e_giant, n_small, n_single) = fa2_blended_layout(
        author_layout_w, set(static_depts), FA2_ITER_AUTHORS, seed
    )
    pos_authors = spread_min_distance(pos_authors, MIN_SEP_AUTHORS, seed)
    logger.info(
        "FA2 over authors: giant %d nodes / %d edges, blended: %d small comps + %d singles, min-sep %.1f, %.1f s",
        n_giant,
        e_giant,
        n_small,
        n_single,
        MIN_SEP_AUTHORS,
        time.time() - t0,
    )

    # --- publication-to-publication graph (shared ITMO authors) -----------------
    pub_pair_w = Counter()
    for _per, plist in author_pubs.items():
        for a, b in combinations(sorted(set(plist)), 2):
            pub_pair_w[(a, b)] += 1

    # Publications get their own FA2 layout (not their authors' centroid,
    # which degenerates for solo-authored pubs). Full w>=1 graph is ~310k
    # edges, so keep only each pub's top-K strongest links.
    t0 = time.time()
    strongest = defaultdict(list)
    for (a, b), w in pub_pair_w.items():
        strongest[a].append((w, b))
        strongest[b].append((w, a))
    pub_layout_w = {}
    for n, lst in strongest.items():
        lst.sort(key=lambda t: (-t[0], t[1]))
        for w, o in lst[:PUB_LAYOUT_TOP_K]:
            pub_layout_w[(n, o) if n < o else (o, n)] = w
    for pair, w in sparse_dept_edges(
        pub_ids,
        pub_primary,
        rng,
        k=PUB_DEPT_EDGE_K,
        weight=PUB_DEPT_EDGE_WEIGHT,
        taper_size=150,
    ).items():
        pub_layout_w[pair] = pub_layout_w.get(pair, 0) + w

    pos_pubs, (n_giant_p, e_giant_p, n_small_p, n_single_p) = fa2_blended_layout(
        pub_layout_w, pub_ids, FA2_ITER_PUBS, seed
    )
    pos_pubs = spread_min_distance(pos_pubs, MIN_SEP_PUBS, seed)
    logger.info(
        "FA2 over publications: giant %d nodes / %d edges, blended: %d small comps + %d singles, min-sep %.1f, %.1f s",
        n_giant_p,
        e_giant_p,
        n_small_p,
        n_single_p,
        MIN_SEP_PUBS,
        time.time() - t0,
    )

    # --- repository edges (shared publications, incl. pubs outside the graph) ---
    repo_all_pubs = defaultdict(set)
    for rid, pid in db["repo_pubs"]:
        repo_all_pubs[rid].add(pid)
    repo_edge_w = Counter()
    for a, b in combinations(sorted(repo_all_pubs), 2):
        shared = len(repo_all_pubs[a] & repo_all_pubs[b])
        if shared:
            repo_edge_w[(a, b)] = shared

    R = nx.Graph()
    R.add_nodes_from(r[0] for r in db["repositories"])
    R.add_weighted_edges_from((a, b, w) for (a, b), w in repo_edge_w.items())
    pos_repos = fit_coords(
        nx.forceatlas2_layout(R, max_iter=FA2_ITER_REPOS, weight="weight", seed=seed)
    )

    # --- nodes ------------------------------------------------------------------
    pubs_count = {per: len(set(author_pubs.get(per, []))) for per in static_depts}
    rank_a = dense_rank(pubs_count)
    authors = []
    for row in db["persons"]:
        pid_ = row["id"]
        x, y = pos_authors[pid_]
        author = {
            "key": pid_,
            "kind": "author",
            "dept": g(author_dept[pid_]),
            "label": author_label(
                row["surname_ru"],
                row["first_name_ru"],
                row["second_name_ru"],
                row["name_en"],
                row.get("name_ru"),
                public=public,
            ),
            "pubs_count": pubs_count[pid_],
            "rank": rank_a[pid_],
            "gx": x,
            "gy": y,
        }
        if not public:
            author["name_en"] = row["name_en"] or ""
            author["name_ru"] = row.get("name_ru") or ""
            author["name_variants"] = author_variants(row)
            author["degree"] = row["degree"] or ""
            author["github"] = row["github"] or ""
            author["orcid"] = row.get("orcid") or ""
        authors.append(author)

    stars = {r[0]: (r[4] or 0) for r in db["repositories"]}
    rank_r = dense_rank(stars)
    repos = []
    for rid, name, url, descr, stars_num, owner in db["repositories"]:
        x, y = pos_repos[rid]
        repos.append(
            {
                "key": rid,
                "kind": "repo",
                "dept": g(repo_dept[rid]),
                "label": name or "",
                "description": descr or "",
                "stars": stars_num or 0,
                "owner": owner or "",
                "url": url or "",
                "rank": rank_r[rid],
                "gx": x,
                "gy": y,
            }
        )

    n_authors_of = {pid: len(set(pub_authors[pid])) for pid in pub_ids}
    rank_p = dense_rank(n_authors_of)
    pubs = []
    for pid, _, _, _, _, year, _, _ in pubs_rows:
        x, y = pos_pubs[pid]
        depts_all = sorted({g(d) for d in pub_dept_rows.get(pid, [])} | {g(pub_primary[pid])})
        pubs.append(
            {
                "key": pid,
                "kind": "pub",
                "dept": g(pub_primary[pid]),
                "depts": depts_all,
                "year": year,
                "n_authors": n_authors_of[pid],
                "rank": rank_p[pid],
                "gx": x,
                "gy": y,
            }
        )

    # --- edges ------------------------------------------------------------------
    coauth_edges = [{"s": a, "t": b, "w": w} for (a, b), w in coauth.items() if w >= COAUTH_MIN_W]

    pub_edges = [
        {"s": a, "t": b, "w": w} for (a, b), w in pub_pair_w.items() if w >= PUB_EDGE_MIN_W
    ]

    repo_edges = [{"s": a, "t": b, "w": w} for (a, b), w in repo_edge_w.items()]

    dept_pair_w = Counter()
    for pid in pub_ids:
        ds = sorted({g(author_dept[per]) for per in pub_authors[pid]} - {no_dept_gid})
        for a, b in combinations(ds, 2):
            dept_pair_w[(a, b)] += 1
    dept_edges = [{"s": a, "t": b, "w": w} for (a, b), w in dept_pair_w.items()]

    repo_author_edges = [
        {"s": rid, "t": per, "role": role}
        for rid, per, role in db["repo_persons"]
        if per in static_depts
    ]
    repo_pub_edges = [{"s": rid, "t": pid} for rid, pid in db["repo_pubs"] if pid in pub_ids]
    all_edges = [{"s": per, "t": pid} for pid, per in db["authorship"]]

    logger.info(
        "Edges: coauth %d, pub %d, repo %d, dept %d, repo-author %d, repo-pub %d, authorship %d",
        len(coauth_edges),
        len(pub_edges),
        len(repo_edges),
        len(dept_edges),
        len(repo_author_edges),
        len(repo_pub_edges),
        len(all_edges),
    )

    return {
        "departments": departments,
        "dept_edges": dept_edges,
        "authors": authors,
        "coauth_edges": coauth_edges,
        "repos": repos,
        "repo_edges": repo_edges,
        "repo_author_edges": repo_author_edges,
        "repo_pub_edges": repo_pub_edges,
        "pubs": pubs,
        "pub_edges": pub_edges,
        "all_edges": all_edges,
    }


def build_search_detail(db, graph):
    """Publication details for graph-search.js (loaded after the map)."""
    pub_ids = {p["key"] for p in graph["pubs"]}
    detail = []
    for pid, title, journal, doi, _, _, has_code, code_url in db["publications"]:
        if pid not in pub_ids:
            continue
        try:
            urls = json.loads(code_url) if code_url else []
        except json.JSONDecodeError:
            urls = []
        title = title or ""
        if len(title) > 200:
            title = title[:199] + "…"
        detail.append(
            {
                "key": pid,
                "label": title,
                "journal": journal or "",
                "doi": doi or "",
                "has_code": bool(has_code),
                "code_url": urls if isinstance(urls, list) else [urls],
            }
        )
    return detail


def dump_js(data, prefix: str, suffix: str, path: Path):
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    path.write_text(prefix + payload + suffix, encoding="utf-8")
    logger.info("Wrote %s (%.1f MB)", path, path.stat().st_size / 1e6)


def write_graph_files(snapshot_path: Path, out_dir: Path, *,
                      seed: int = 42, public: bool = False) -> dict[str, int]:
    """Build the map's two data files from a snapshot.

    The work `main()` used to hold inline, so that a caller which is not a
    command line — the maintenance worker — can rebuild the map without
    spawning a process and building an argument list out of a form.

    Args:
        snapshot_path: A graph snapshot written by `pauk cache export`.
        out_dir: Where graph-data.js and graph-search.js go. Created if it
            is not there.
        seed: FA2 layout seed. The same seed gives the same layout, so a
            rebuild does not shuffle a map people have learned to read.
        public: Drop personal fields, for a build that leaves the corporate
            network.

    Returns:
        How much was written, for the run to report.
    """
    from pauk.cache.graph_snapshot import read_snapshot

    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    db = read_snapshot(snapshot_path)

    graph = build_graph_data(db, seed=seed, public=public)
    dump_js(graph, "window.GRAPH=", "", out_dir / "graph-data.js")

    detail = build_search_detail(db, graph)
    dump_js(
        detail,
        "(function(){var d=",
        ";if(typeof window._onDetailReady==='function')window._onDetailReady(d);"
        "else window._pendingDetail=d;})();",
        out_dir / "graph-search.js",
    )
    logger.info("Done in %.1f s", time.time() - started)
    return {
        "map_authors": len(graph["authors"]),
        "map_pubs": len(graph["pubs"]),
        "map_repos": len(graph["repos"]),
        "map_departments": len(graph["departments"]),
        "map_edges": len(graph["all_edges"]),
    }


def main():
    from pauk.settings import settings

    parser = argparse.ArgumentParser(description="Static data generation for the web visualization")
    parser.add_argument(
        "--public",
        action="store_true",
        help="drop personal fields (name_en, name_ru, name_variants, degree, "
        "github, orcid) from every author — for a build that leaves the "
        "corporate network, e.g. the GitHub Pages deploy",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="where to write graph-data.js and graph-search.js "
        f"(default: {settings.map_out_dir(True)} with --public, "
        f"else {settings.map_out_dir(False)})",
    )
    parser.add_argument("--seed", type=int, default=42, help="FA2 layout seed")
    parser.add_argument(
        "--cache",
        type=Path,
        default=settings.cache_dir / "graph_snapshot.json",
        help="path to a graph snapshot created by 'pauk cache export'",
    )
    args = parser.parse_args()
    if args.out_dir is None:
        args.out_dir = settings.map_out_dir(args.public)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    write_graph_files(args.cache, args.out_dir, seed=args.seed, public=args.public)


if __name__ == "__main__":
    main()
