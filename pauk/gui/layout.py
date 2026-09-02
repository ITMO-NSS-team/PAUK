"""Pure layout math: FA2 positioning, coordinate fitting, collision spread.
No knowledge of authors/pubs/repos as domain concepts — just node ids and
edge weights. See generate_data.py for how these are called per entity type.
"""

import colorsys
import math
import random
from collections import Counter, defaultdict
from itertools import combinations

import networkx as nx
import numpy as np
from scipy.spatial import cKDTree  # type: ignore

from .config import DEPT_EDGE_K, DEPT_EDGE_WEIGHT


def golden_color(i: int) -> str:
    """Department color: golden-ratio hue step, HLS l=0.6 s=0.4."""
    hue = (i * 0.618033988749895) % 1.0
    r, g, b = colorsys.hls_to_rgb(hue, 0.6, 0.4)
    return f"#{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"


def dense_rank(values: dict) -> dict:
    """rank = dense rank of the metric / number of unique values, rounded to 3."""
    uniq = sorted(set(values.values()))
    pos = {v: (i + 1) / len(uniq) for i, v in enumerate(uniq)}
    return {k: round(pos[v], 3) for k, v in values.items()}


# Frontend coordinate space: 0..1000 (core.js: S = 1000)
COORD_MIN, COORD_MAX = 30.0, 970.0


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
    return {k: (round(500.0 + (x - cx) * scale, 1), round(500.0 + (y - cy) * scale, 1)) for k, (x, y) in pos.items()}


def spread_min_distance(pos, d_min, seed, iters=800):
    """Push apart any pair of nodes closer than d_min — converged FA2 clusters
    are dense enough to render as solid filled discs otherwise."""
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
    return {k: (round(float(x), 1), round(float(y), 1)) for k, (x, y) in zip(keys, P, strict=True)}


def co_membership_weights(groups, weight, cap=None):
    """Project "these nodes share a thing" onto weighted node-node edges.

    Each group of `k` members is a clique of k*(k-1)/2 pairs, so a raw count
    lets one large group outweigh every small one put together. Newman's
    share — `weight / (k - 1)` per pair — keeps a member's total pull on its
    group constant regardless of the group's size, which is what makes two
    repositories sharing a single publication rank above two of the fifty
    a lab account happens to hold.

    `cap` drops groups larger than it outright: past some size the shared
    thing stops being evidence (a bot credited on two hundred repositories)
    and only smears the layout.
    """
    pair_w = defaultdict(float)
    for members in groups:
        members = sorted(set(members))
        if len(members) < 2 or (cap is not None and len(members) > cap):
            continue
        share = weight / (len(members) - 1)
        for a, b in combinations(members, 2):
            pair_w[(a, b)] += share
    return pair_w


def top_k_edges(pair_w, k):
    """Keep each node's k strongest edges; the result is their union, so a node
    can still end up with more than k. Without this the densest groups render
    as solid ink and hide every edge that carries actual information."""
    strongest = defaultdict(list)
    for (a, b), w in pair_w.items():
        strongest[a].append((w, b))
        strongest[b].append((w, a))
    kept = {}
    for node, lst in strongest.items():
        lst.sort(key=lambda t: (-t[0], t[1]))
        for w, other in lst[:k]:
            kept[(node, other) if node < other else (other, node)] = w
    return kept


def sparse_dept_edges(all_ids, dept_of, rng, k=DEPT_EDGE_K, weight=DEPT_EDGE_WEIGHT, taper_size=None):
    """Weak "shared department" edges: each node ties to k random peers from
    its department. Sparse random graph -> organic blob under FA2; a
    per-department hub/anchor node instead arranges its leaves in a perfect
    circle (rings were exactly the artifact this replaces).

    taper_size: departments larger than this get proportionally weaker
    edges, so already-large depts don't flatten into a featureless disc."""
    # all_ids is typically a set — sorted so department grouping order (and
    # thus how much of `rng`'s shared state each department consumes) is
    # the same on every run of the same seed, not shuffled by string-hash
    # randomization between processes.
    by_dept = defaultdict(list)
    for i in sorted(all_ids):
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


# Sigma of the blend-in scatter (final 0..1000 units) and min spacing between
# stranded nodes (grid cell size) for fa2_blended_layout's blend-in pass.
STRANDED_JITTER = 55.0
STRANDED_MIN_SEP = 7.0


def fa2_blended_layout(edge_weights, all_ids, max_iter, seed):
    """FA2 over the GIANT connected component only, then blend everything else
    in afterward. This is essential, not an optimization: disconnected
    components only repel each other and drift apart without bound as
    iterations grow, so the real content gets crushed to a dot on rescale
    ("everything piled in the center") if FA2 runs on the full graph.

    Small components (>=2 nodes) land together as one tight patch so
    collaborators stay adjacent; true singletons scatter individually with
    jitter on a coarse occupancy grid so they don't clump or ring."""
    # all_ids is typically a set — sorted so node insertion order (which the
    # seeded initial FA2 positions get assigned by, via enumerate(G)) is the
    # same on every run of the same seed, not shuffled by string-hash
    # randomization between processes.
    G = nx.Graph()
    G.add_nodes_from(sorted(all_ids))
    G.add_weighted_edges_from((a, b, w) for (a, b), w in edge_weights.items())
    comps = list(nx.connected_components(G))
    giant = max(comps, key=len) if comps else set()
    if len(giant) < 2:
        giant = set()
    small = sorted((c for c in comps if c is not giant and len(c) >= 2), key=len, reverse=True)
    singles = sorted(n for c in comps if c is not giant and len(c) == 1 for n in c)

    pos = {}
    if giant:
        # G.subgraph() builds its view off a set internally, so its node
        # order is still hash-randomization-dependent even when `giant` is
        # sorted first — copy into a fresh graph with an explicit order instead.
        sub = nx.Graph()
        sub.add_nodes_from(sorted(giant))
        sub.add_weighted_edges_from((a, b, d["weight"]) for a, b, d in G.edges(data=True) if a in giant and b in giant)
        pos = fit_coords(nx.forceatlas2_layout(sub, max_iter=max_iter, weight="weight", seed=seed))

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
        sx, sy = rng.uniform(0.55, 1.6), rng.uniform(0.55, 1.6)  # stretch/rotate so patches aren't neat circles
        ang = rng.uniform(0.0, math.pi)
        cos_a, sin_a = math.cos(ang), math.sin(ang)

        def patch_point(radius=radius, sx=sx, sy=sy, ccx=ccx, ccy=ccy, cos_a=cos_a, sin_a=sin_a):
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


def majority_vote(value_lists):
    """The value most of the lists carry; ties broken by id (not by global
    popularity — that would create a rich-get-richer feedback loop favoring
    already-large groups).

    Used for departments and for publication fields alike: both are read off
    rows that arrive from Neo4j in no defined order, so a tie has to be
    settled by the value itself or the answer moves between runs.
    """
    cnt = Counter()
    for values in value_lists:
        for value in values:
            cnt[value] += 1
    if not cnt:
        return None
    return sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
