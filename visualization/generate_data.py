"""Static data generation for the web visualization.

Reads Neo4j (graph loaded by scripts/graph_loader.py, connection via
NEO4J_URI/NEO4J_USER/NEO4J_PASSWORD in .env) and generates, in one pass,
the ready-made JS files served to the web frontend as-is (no backend):

  web/graph-data.js    -> window.GRAPH  (nodes, edges, layout, departments)
  web/graph-search.js  -> publication details (label/journal/doi/has_code/code_url),
                          loaded after the map via _onDetailReady

Layout: each entity type gets its own ForceAtlas2 (networkx) run over its
GIANT connected component only, weighted by its own proximity measure —
authors by joint publications + joint repositories + shared department;
publications by shared authors + shared department; repositories by shared
publications. "Shared department" is a sparse random peer graph
(sparse_dept_edges), small disconnected components blend in as tight
patches, singletons scatter inside the cloud (fa2_blended_layout), and a
collision pass (spread_min_distance) enforces minimum node spacing.

Department assignment rules:
  publication -> all departments of its ITMO authors (the `depts` field) +
                 one "primary" (`dept`, by majority vote) for color/map;
  author      -> department of their most recent publication;
  repository  -> majority vote over the primary departments of its
                 publications, fallback — repository_departments.

Usage:
  uv run python visualization/generate_data.py [--out-dir web] [--seed 42]
"""

import argparse
import colorsys
import json
import logging
import math
import os
import random
import time
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

import networkx as nx
from dotenv import load_dotenv
from neo4j import GraphDatabase

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR_DEFAULT = Path(__file__).resolve().parent / "web"

load_dotenv(REPO_ROOT / ".env")
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password")

NO_DEPT_NAME = "Без департамента"
NO_DEPT_COLOR = "#8a8f98"

# Edge export thresholds (the layout is computed over the full graph)
COAUTH_MIN_W = 2       # min joint publications for an author-author edge
PUB_EDGE_MIN_W = 3     # min shared ITMO authors for a publication-publication edge

# Frontend coordinate space: 0..1000 (core.js: S = 1000)
COORD_MIN, COORD_MAX = 30.0, 970.0

FA2_ITER_AUTHORS = int(os.getenv("PAUK_FA2_ITER_AUTHORS", "300"))
FA2_ITER_PUBS = int(os.getenv("PAUK_FA2_ITER_PUBS", "250"))
FA2_ITER_REPOS = int(os.getenv("PAUK_FA2_ITER_REPOS", "100"))
PUB_LAYOUT_TOP_K = 6   # strongest shared-author edges kept per publication for layout


def golden_color(i: int) -> str:
    """Department color: golden-ratio hue step, HLS l=0.6 s=0.4."""
    hue = (i * 0.618033988749895) % 1.0
    r, g, b = colorsys.hls_to_rgb(hue, 0.6, 0.4)
    return "#{:02x}{:02x}{:02x}".format(round(r * 255), round(g * 255), round(b * 255))


def dense_rank(values: dict) -> dict:
    """rank = dense rank of the metric / number of unique values, rounded to 3."""
    uniq = sorted(set(values.values()))
    pos = {v: (i + 1) / len(uniq) for i, v in enumerate(uniq)}
    return {k: round(pos[v], 3) for k, v in values.items()}


def author_label(surname_ru, first_name_ru, second_name_ru, name_en) -> str:
    """Surname + initials from Russian name fields, fallback — name_en."""
    if surname_ru:
        label = surname_ru
        if first_name_ru:
            label += f" {first_name_ru[0]}."
            if second_name_ru:
                label += f"{second_name_ru[0]}."
        elif second_name_ru:
            # first_name_ru is empty, but second_name_ru holds the initials
            label += f" {second_name_ru[0]}."
        return label
    return name_en or ""


def fit_coords(pos: dict) -> dict:
    """Fit FA2 coordinates into [COORD_MIN, COORD_MAX], preserving aspect ratio."""
    if not pos:
        return {}
    pos = {k: (float(p[0]), float(p[1])) for k, p in pos.items()}
    xs = [p[0] for p in pos.values()]
    ys = [p[1] for p in pos.values()]
    span = max(max(xs) - min(xs), max(ys) - min(ys)) or 1.0
    scale = (COORD_MAX - COORD_MIN) / span
    cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
    return {
        k: (round(500.0 + (x - cx) * scale, 1), round(500.0 + (y - cy) * scale, 1))
        for k, (x, y) in pos.items()
    }


DEPT_EDGE_K = 3        # random same-department peers each node is tied to
DEPT_EDGE_WEIGHT = 1.0  # comparable to real edges (joint pubs start at 1.0)
# Publications use a much weaker department term: a dense random same-dept
# graph is structureless and relaxes into a featureless ball ("circle") under
# FA2; with k=1/w=0.5 the real shared-author links shape each cluster instead.
PUB_DEPT_EDGE_K = 1
PUB_DEPT_EDGE_WEIGHT = 0.5
STRANDED_JITTER = 55.0  # sigma of the blend-in scatter, in final 0..1000 units
STRANDED_MIN_SEP = 7.0  # min spacing between stranded nodes (grid cell size)
MIN_SEP_AUTHORS = float(os.getenv("PAUK_MIN_SEP_AUTHORS", "4.5"))
MIN_SEP_PUBS = float(os.getenv("PAUK_MIN_SEP_PUBS", "3.5"))


def spread_min_distance(pos, d_min, seed, iters=800):
    """Collision-removal pass: iteratively push apart any pair of nodes closer
    than d_min. Converged FA2 clusters are so dense they render as solid
    filled discs ("circles"); spacing nodes out turns them into readable
    point clouds while keeping the global structure — pushes are tiny and
    local, only ever between already-overlapping neighbors."""
    import numpy as np
    from scipy.spatial import cKDTree

    keys = list(pos)
    P = np.array([pos[k] for k in keys], dtype=float)
    rng = np.random.RandomState(seed)
    for _ in range(iters):
        pairs = cKDTree(P).query_pairs(d_min, output_type="ndarray")
        # a handful of stragglers (nodes pinned at the map border) is fine
        if len(pairs) <= max(2, len(keys) // 2000):
            break
        delta = P[pairs[:, 0]] - P[pairs[:, 1]]
        dist = np.hypot(delta[:, 0], delta[:, 1])
        coincident = dist < 1e-9
        if coincident.any():
            delta[coincident] = rng.uniform(-1, 1, (int(coincident.sum()), 2))
            dist[coincident] = np.hypot(delta[coincident, 0], delta[coincident, 1])
        dirv = delta / dist[:, None]
        push = ((d_min - dist) * 0.45)[:, None] * dirv
        np.add.at(P, pairs[:, 0], push)
        np.subtract.at(P, pairs[:, 1], push)
        np.clip(P, 30.0, 970.0, out=P)
    return {k: (round(float(x), 1), round(float(y), 1)) for k, (x, y) in zip(keys, P)}


def sparse_dept_edges(all_ids, dept_of, rng, k=DEPT_EDGE_K, weight=DEPT_EDGE_WEIGHT,
                      taper_size=None):
    """Weak "shared department" edges for the layout: each node is tied to k
    random peers from its department. A sparse random graph clusters into an
    organic blob under FA2 — unlike a per-department hub/anchor node, whose
    star topology arranges leaves in a perfect circle around it (rings were
    exactly the visual artifact this replaces).

    taper_size: if set, departments larger than this get proportionally
    weaker edges (weight * taper_size/size). Big departments are already
    held together by their real edges; at full strength the random dept
    graph homogenizes them into a featureless disc."""
    by_dept = defaultdict(list)
    for i in all_ids:
        d = dept_of.get(i)
        if d:
            by_dept[d].append(i)
    edges = {}
    for members in by_dept.values():
        if len(members) < 2:
            continue
        w = weight
        if taper_size and len(members) > taper_size:
            w = weight * taper_size / len(members)
        members = sorted(members)
        for i in members:
            others = [m for m in members if m != i]
            for j in rng.sample(others, min(k, len(others))):
                edges[(i, j) if i < j else (j, i)] = w
    return edges


def fa2_blended_layout(edge_weights, all_ids, max_iter, seed):
    """FA2 over the GIANT connected component only, then blend everything
    else into the resulting cloud.

    Restricting FA2 to the giant component is essential: disconnected
    components feel only repulsion from each other and drift apart without
    bound as iterations grow — the giant component then gets crushed into a
    tiny blob when coordinates are rescaled (this was the "everything piled
    in the center" artifact).

    Blending (mimics the original lost layout, where isolated authors were
    interspersed through the whole map — no center pile, no ring):
      - small components (>=2 nodes) stay together as one tight patch near a
        random crowd position, so nodes that DO collaborate remain adjacent;
      - true singletons land near a random crowd position with jitter,
        spaced out on a coarse occupancy grid so they don't clump."""
    G = nx.Graph()
    G.add_nodes_from(all_ids)
    G.add_weighted_edges_from((a, b, w) for (a, b), w in edge_weights.items())
    comps = list(nx.connected_components(G))
    giant = max(comps, key=len) if comps else set()
    if len(giant) < 2:
        giant = set()
    small = sorted((c for c in comps if c is not giant and len(c) >= 2), key=len, reverse=True)
    singles = sorted(n for c in comps if c is not giant and len(c) == 1 for n in c)

    pos = {}
    if giant:
        pos = fit_coords(
            nx.forceatlas2_layout(
                G.subgraph(giant), max_iter=max_iter, weight="weight", seed=seed
            )
        )

    rng = random.Random(seed + 1)
    crowd = list(pos.values()) or [(500.0, 500.0)]
    occupied = set()
    cell = STRANDED_MIN_SEP

    def place(x, y):
        x = min(970.0, max(30.0, x))
        y = min(970.0, max(30.0, y))
        occupied.add((int(x // cell), int(y // cell)))
        return round(x, 1), round(y, 1)

    def free_spot(gen):
        for _attempt in range(60):
            x, y = gen()
            x = min(970.0, max(30.0, x))
            y = min(970.0, max(30.0, y))
            if (int(x // cell), int(y // cell)) not in occupied:
                break
        return place(x, y)

    for comp in small:
        ax, ay = rng.choice(crowd)
        ccx = min(940.0, max(60.0, rng.gauss(ax, STRANDED_JITTER)))
        ccy = min(940.0, max(60.0, rng.gauss(ay, STRANDED_JITTER)))
        radius = 6.0 + 2.2 * math.sqrt(len(comp))
        # random stretch + rotation per patch: an isotropic Gaussian would
        # draw every small component as a neat round spot
        sx, sy = rng.uniform(0.55, 1.6), rng.uniform(0.55, 1.6)
        ang = rng.uniform(0.0, math.pi)
        cos_a, sin_a = math.cos(ang), math.sin(ang)

        def patch_point():
            dx, dy = rng.gauss(0, radius * sx), rng.gauss(0, radius * sy)
            return ccx + dx * cos_a - dy * sin_a, ccy + dx * sin_a + dy * cos_a

        for n in sorted(comp):
            pos[n] = free_spot(patch_point)

    for n in singles:
        def gen():
            ax, ay = rng.choice(crowd)
            return rng.gauss(ax, STRANDED_JITTER), rng.gauss(ay, STRANDED_JITTER)
        pos[n] = free_spot(gen)

    stats = (len(giant), G.subgraph(giant).number_of_edges(), len(small), len(singles))
    return pos, stats


def majority_dept(dept_lists):
    """Department by majority vote; ties broken by id (not by global popularity —
    that would create a rich-get-richer feedback loop favoring already-large depts)."""
    cnt = Counter()
    for depts in dept_lists:
        for d in depts:
            cnt[d] += 1
    if not cnt:
        return None
    return sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


CYPHER_RETRIES = 5


def cypher(driver, query, **params):
    """Managed read with retries: driver.execute_query() re-runs the whole
    query on transient/connection failures, and we add an outer retry loop
    with backoff on top — over a VPN a single dropped connection must not
    kill a multi-minute export (session.run() has no retry at all: one
    mid-stream stall means SessionExpired and a dead run)."""
    from neo4j.exceptions import ServiceUnavailable, SessionExpired, TransientError

    for attempt in range(1, CYPHER_RETRIES + 1):
        try:
            t0 = time.time()
            records, _, _ = driver.execute_query(query, **params)
            logger.info("запрос вернул %d строк за %.1f c: %s…",
                        len(records), time.time() - t0, query.lstrip()[:60])
            return [tuple(r.values()) for r in records]
        except (ServiceUnavailable, SessionExpired, TransientError, OSError) as exc:
            if attempt == CYPHER_RETRIES:
                raise
            wait = min(60, 5 * attempt)
            logger.warning("запрос упал (%s: %s), попытка %d/%d, пауза %d c",
                           type(exc).__name__, exc, attempt, CYPHER_RETRIES, wait)
            time.sleep(wait)


def load_db(driver):
    """Read everything we need from Neo4j into the same flat structures
    load_db() used to return from SQLite, so build_graph_data() below is
    untouched.

    Two things aren't plain column reads anymore, because the graph model
    stores them as relationships rather than string columns:
      - an author's departments -> (:Person:Itmo)-[:BELONGS_TO]->(:Department)
      - a repo's owner          -> (:Repository)-[:OWNED_BY]->(:GitHubProfile)
    """
    db = {}

    db["persons"] = cypher(
        driver,
        "MATCH (p:Person:Itmo) "
        "RETURN p.id AS id, p.first_name_ru AS first_name_ru, "
        "       p.second_name_ru AS second_name_ru, p.surname_ru AS surname_ru, "
        "       p.name_en AS name_en, p.degree AS degree, p.github AS github",
    )

    db["publications"] = cypher(
        driver,
        "MATCH (pub:Publication) "
        "RETURN pub.id AS id, pub.title AS title, pub.journal AS journal, "
        "       pub.doi AS doi, toString(pub.publication_date) AS publication_date, "
        "       pub.year AS year, pub.has_code AS has_code, pub.code_url AS code_url",
    )

    db["repositories"] = cypher(
        driver,
        "MATCH (r:Repository) "
        "OPTIONAL MATCH (r)-[:OWNED_BY]->(gh:GitHubProfile) "
        "RETURN r.id AS id, r.name AS name, r.url AS url, "
        "       r.description AS description, r.stars_num AS stars_num, gh.login AS owner",
    )

    db["departments"] = cypher(
        driver,
        "MATCH (d:Department) RETURN d.id AS id, coalesce(d.name_ru, d.name_en) AS name",
    )

    # Person:Itmo label already means "ITMO author" — no id-prefix filter needed.
    db["authorship"] = cypher(
        driver,
        "MATCH (p:Person:Itmo)-[:AUTHORED]->(pub:Publication) "
        "RETURN pub.id AS pid, p.id AS per",
    )

    db["person_depts"] = cypher(
        driver,
        "MATCH (p:Person:Itmo)-[:BELONGS_TO]->(d:Department) "
        "RETURN p.id AS per, d.id AS did",
    )

    # No rowid equivalent on relationships; ordered by id for determinism
    # (only used as a rare fallback when the majority-vote rule finds nothing).
    db["pub_depts"] = cypher(
        driver,
        "MATCH (pub:Publication)-[:PRODUCED_BY]->(d:Department) "
        "RETURN pub.id AS pid, d.id AS did ORDER BY d.id",
    )

    db["repo_pubs"] = cypher(
        driver,
        "MATCH (r:Repository)-[:IMPLEMENTS]->(pub:Publication) "
        "RETURN r.id AS rid, pub.id AS pid",
    )

    db["repo_persons"] = cypher(
        driver,
        "MATCH (p:Person:Itmo)-[rel:CONTRIBUTED_TO]->(r:Repository) "
        "RETURN r.id AS rid, p.id AS per, rel.role AS role",
    )

    db["repo_depts"] = cypher(
        driver,
        "MATCH (r:Repository)-[:DEVELOPED_BY]->(d:Department) "
        "RETURN r.id AS rid, d.id AS did ORDER BY d.id",
    )

    return db


def build_graph_data(db, seed: int):
    dept_name = dict(db["departments"])

    # --- authorship: only publications with at least one ITMO author ----------
    pub_authors = defaultdict(list)
    author_pubs = defaultdict(list)
    for pid, per in db["authorship"]:
        pub_authors[pid].append(per)
        author_pubs[per].append(pid)

    pubs_rows = [r for r in db["publications"] if r[0] in pub_authors]
    pub_ids = {r[0] for r in pubs_rows}
    logger.info(
        "publications with ITMO authors: %d of %d", len(pubs_rows), len(db["publications"])
    )

    # --- static author departments (:Person:Itmo)-[:BELONGS_TO]->(:Department) -
    static_depts = {pid_: [] for pid_, *_ in db["persons"]}
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
        for pid in sorted(author_pubs.get(per, []), key=lambda p: (pub_date.get(p, ""), p), reverse=True):
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
    # Departments referenced only by publications/repositories (n=0)
    # must still get a graph id
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
            "color": NO_DEPT_COLOR,
            "n": n_auth[no_dept_gid] + n_pub[no_dept_gid] + n_repo[no_dept_gid],
            "n_authors": n_auth[no_dept_gid],
            "n_pubs": n_pub[no_dept_gid],
            "n_repos": n_repo[no_dept_gid],
        }
    )
    logger.info("departments: %d (+ \"%s\")", len(ordered), NO_DEPT_NAME)

    # --- co-authorship graph and FA2 layout -------------------------------------
    # Proximity measure per the original design: joint publications + joint
    # repositories + shared department. The exported coauth_edges (visible
    # lines in the UI) stay pure joint-publication counts — this richer graph
    # is layout-only scaffolding for positioning.
    coauth = Counter()
    for pid, pers in pub_authors.items():
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
        "FA2 over authors: giant %d nodes / %d edges, blended: %d small comps + %d singles, "
        "min-sep %.1f, %.1f s",
        n_giant, e_giant, n_small, n_single, MIN_SEP_AUTHORS, time.time() - t0,
    )

    # --- publication-to-publication graph (shared ITMO authors) -----------------
    pub_pair_w = Counter()
    for per, plist in author_pubs.items():
        for a, b in combinations(sorted(set(plist)), 2):
            pub_pair_w[(a, b)] += 1

    # Publications get their own FA2 layout too — shared authors + shared
    # department — instead of inheriting their authors' centroid. A centroid
    # degenerates badly for solo-authored publications, which just collapse
    # onto whatever position their one author happens to have.
    # Even one shared author is meaningful proximity, but the full w>=1 graph
    # is ~310k edges; keeping each publication's top-K strongest edges
    # preserves local structure at a fraction of the cost.
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
        pub_ids, pub_primary, rng,
        k=PUB_DEPT_EDGE_K, weight=PUB_DEPT_EDGE_WEIGHT, taper_size=150,
    ).items():
        pub_layout_w[pair] = pub_layout_w.get(pair, 0) + w

    pos_pubs, (n_giant_p, e_giant_p, n_small_p, n_single_p) = fa2_blended_layout(
        pub_layout_w, pub_ids, FA2_ITER_PUBS, seed
    )
    pos_pubs = spread_min_distance(pos_pubs, MIN_SEP_PUBS, seed)
    logger.info(
        "FA2 over publications: giant %d nodes / %d edges, blended: %d small comps + %d singles, "
        "min-sep %.1f, %.1f s",
        n_giant_p, e_giant_p, n_small_p, n_single_p, MIN_SEP_PUBS, time.time() - t0,
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
    for pid_, fn, sn, sur, name_en, degree, github in db["persons"]:
        x, y = pos_authors[pid_]
        authors.append(
            {
                "key": pid_,
                "kind": "author",
                "dept": g(author_dept[pid_]),
                "label": author_label(sur, fn, sn, name_en),
                "degree": degree or "",
                "github": github or "",
                "pubs_count": pubs_count[pid_],
                "rank": rank_a[pid_],
                "gx": x,
                "gy": y,
            }
        )

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
        depts_all = sorted(
            {g(d) for d in pub_dept_rows.get(pid, [])} | {g(pub_primary[pid])}
        )
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
    coauth_edges = [
        {"s": a, "t": b, "w": w} for (a, b), w in coauth.items() if w >= COAUTH_MIN_W
    ]

    pub_edges = [
        {"s": a, "t": b, "w": w} for (a, b), w in pub_pair_w.items() if w >= PUB_EDGE_MIN_W
    ]

    repo_edges = [{"s": a, "t": b, "w": w} for (a, b), w in repo_edge_w.items()]

    dept_pair_w = Counter()
    for pid in pub_ids:
        ds = sorted(
            {g(author_dept[per]) for per in pub_authors[pid]} - {no_dept_gid}
        )
        for a, b in combinations(ds, 2):
            dept_pair_w[(a, b)] += 1
    dept_edges = [{"s": a, "t": b, "w": w} for (a, b), w in dept_pair_w.items()]

    repo_author_edges = [
        {"s": rid, "t": per, "role": role}
        for rid, per, role in db["repo_persons"]
        if per in static_depts
    ]
    repo_pub_edges = [
        {"s": rid, "t": pid} for rid, pid in db["repo_pubs"] if pid in pub_ids
    ]
    all_edges = [{"s": per, "t": pid} for pid, per in db["authorship"]]

    logger.info(
        "edges: coauth %d, pub %d, repo %d, dept %d, repo-author %d, repo-pub %d, authorship %d",
        len(coauth_edges), len(pub_edges), len(repo_edges), len(dept_edges),
        len(repo_author_edges), len(repo_pub_edges), len(all_edges),
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
    logger.info("wrote %s (%.1f MB)", path, path.stat().st_size / 1e6)


def main():
    parser = argparse.ArgumentParser(description="Static data generation for the web visualization")
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR_DEFAULT,
                        help="where to write graph-data.js and graph-search.js")
    parser.add_argument("--seed", type=int, default=42, help="FA2 layout seed")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    args.out_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    driver = GraphDatabase.driver(
        NEO4J_URI,
        auth=(NEO4J_USER, NEO4J_PASSWORD),
        keep_alive=True,
        connection_timeout=30.0,
        max_transaction_retry_time=120.0,
    )
    try:
        driver.verify_connectivity()
        db = load_db(driver)
    finally:
        driver.close()

    graph = build_graph_data(db, seed=args.seed)
    dump_js(graph, "window.GRAPH=", "", args.out_dir / "graph-data.js")

    detail = build_search_detail(db, graph)
    dump_js(
        detail,
        "(function(){var d=",
        ";if(typeof window._onDetailReady==='function')window._onDetailReady(d);"
        "else window._pendingDetail=d;})();",
        args.out_dir / "graph-search.js",
    )

    logger.info("done in %.1f s", time.time() - t0)


if __name__ == "__main__":
    main()
