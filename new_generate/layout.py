"""Graph layout: FA2 positioning, coordinate fitting, collision spreading -
plus assembling all three layouts (authors/publications/repositories) from
the snapshot. Knows nothing about personal author/publication/repository
fields - only node ids (always `str`) and edge weights. Used as input for
building nodes/edges - see `nodes.py`/`edges.py`.
"""

from __future__ import annotations

import logging
import math
import random
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations

import networkx as nx
import numpy as np
from scipy.spatial import cKDTree  # type: ignore

from .authorship import Authorship
from .config import EDGE_THRESHOLDS, FA2_ITERATIONS, MIN_SEPARATION, SYNTHETIC_DEPT_EDGES
from .departments import DepartmentAssignment

logger = logging.getLogger(__name__)

# Frontend coordinate space: a 0..1000 canvas, translated to [longitude,
# latitude] by new_gui/src/map/build.ts::toLngLat() for MapLibre. 30/970
# leave a margin from the 0/1000 edges.
COORD_MIN, COORD_MAX = 30.0, 970.0


def fit_coords(pos: Mapping[str, Sequence[float]]) -> dict[str, tuple[float, float]]:
    """Fits FA2 coordinates into [COORD_MIN, COORD_MAX], preserving proportions.

    Args:
        pos: Node positions from `networkx.forceatlas2_layout` (numpy arrays
            `[x, y]`) or plain tuples `(x, y)` - either works, the function
            converts both ends of the pair to `float` up front.

    Returns:
        The same ids, coordinates rescaled and rounded to 0.1.
    """
    if not pos:
        return {}
    pos = {k: (float(p[0]), float(p[1])) for k, p in pos.items()}
    xs = [p[0] for p in pos.values()]
    ys = [p[1] for p in pos.values()]
    span = max(max(xs) - min(xs), max(ys) - min(ys)) or 1.0
    scale = (COORD_MAX - COORD_MIN) / span
    cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
    return {k: (round(500.0 + (x - cx) * scale, 1), round(500.0 + (y - cy) * scale, 1)) for k, (x, y) in pos.items()}


def spread_min_distance(
    pos: dict[str, tuple[float, float]], d_min: float, seed: int, iters: int = 800
) -> dict[str, tuple[float, float]]:
    """Pushes apart any pair of nodes closer than d_min - a converged FA2
    cluster is otherwise dense enough to render as a solid blob instead of a
    cloud of points.

    Args:
        pos: Node positions after layout.
        d_min: Minimum allowed distance between two nodes.
        seed: Random number generator seed (for reproducibility).
        iters: Maximum spreading iterations.

    Returns:
        The same ids, coordinates spread apart and rounded to 0.1.
    """
    keys = list(pos)
    P = np.array([pos[k] for k in keys], dtype=float)
    rng = np.random.RandomState(seed)
    for _ in range(iters):
        pairs = cKDTree(P).query_pairs(d_min, output_type="ndarray")
        # A handful of stragglers (nodes pinned to the canvas edge) is normal.
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
        np.clip(P, COORD_MIN, COORD_MAX, out=P)
    return {k: (round(float(x), 1), round(float(y), 1)) for k, (x, y) in zip(keys, P, strict=True)}


def sparse_dept_edges(
    all_ids: Iterable[str],
    dept_of: dict[str, str | None],
    rng: random.Random,
    k: int = SYNTHETIC_DEPT_EDGES.dept_edge_k,
    weight: float = SYNTHETIC_DEPT_EDGES.dept_edge_weight,
    taper_size: int | None = None,
) -> dict[tuple[str, str], float]:
    """Weak "same department" edges: each node connects to k random
    colleagues in its department. A sparse random graph gives FA2 an organic
    cloud; a hub node per department would instead arrange its leaves in a
    perfect circle (a ring artifact this replaces).

    Args:
        all_ids: All node ids (usually a set - sorted below, see further down).
        dept_of: A node's department by id, `None`/missing means no department.
        rng: Random number generator (one shared across the whole layout run).
        k: How many random department colleagues each node picks.
        weight: Weight of one such edge.
        taper_size: Departments larger than this get proportionally weaker
            edges, so already-large departments don't collapse into a
            shapeless disk.

    Returns:
        Weight for each node pair `(a, b)` with `a < b`.
    """
    # all_ids is usually a set; sorted so that department grouping (and how
    # much shared rng state each department consumes) is identical across
    # runs with the same seed, rather than shuffled by string hash
    # randomization between processes.
    by_dept: dict[str, list[str]] = defaultdict(list)
    for i in sorted(all_ids):
        d = dept_of.get(i)
        if d:
            by_dept[d].append(i)
    edges: dict[tuple[str, str], float] = {}
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


# Jitter sigma when blending stranded nodes in (in final 0..1000 units), and
# the minimum spacing between stragglers (occupancy grid cell size) for the
# blending pass in fa2_blended_layout.
STRANDED_JITTER = 55.0
STRANDED_MIN_SEP = 7.0


def fa2_blended_layout(
    edge_weights: dict[tuple[str, str], float], all_ids: Iterable[str], max_iter: int, seed: int
) -> tuple[dict[str, tuple[float, float]], tuple[int, int, int, int]]:
    """Runs FA2 only on the GIANT connected component; everything else is
    blended in afterward. Not an optimization but a necessity: disconnected
    components only push away from each other and drift apart without bound
    as iterations grow, so the real content collapses to a point once
    rescaled ("everything dumped in the center") if FA2 runs on the full graph.

    Small components (>=2 nodes) settle together as one dense patch, so
    coauthors stay near each other; true singletons are scattered
    individually with jitter on a coarse occupancy grid, so they don't
    clump or form a ring.

    Args:
        edge_weights: Weight for each node pair `(a, b)`.
        all_ids: All node ids, including ones in no edge at all.
        max_iter: ForceAtlas2 iteration count for the giant component.
        seed: Seed (used for both FA2 and blending - the latter uses
            `seed + 1`, so it doesn't exactly repeat FA2's randomness).

    Returns:
        A `(pos, stats)` tuple, where `pos` is positions for every node in
        `all_ids`, and `stats` is `(giant node count, giant edge count,
        small component count, singleton count)` for logging.
    """
    # all_ids is usually a set; sorted so that node insertion order (which
    # FA2's initial positions depend on for a given seed, via enumerate(G))
    # is identical across runs with the same seed, rather than shuffled by
    # string hash randomization between processes.
    G = nx.Graph()
    G.add_nodes_from(sorted(all_ids))
    G.add_weighted_edges_from((a, b, w) for (a, b), w in edge_weights.items())
    comps = list(nx.connected_components(G))
    giant = max(comps, key=len) if comps else set()
    if len(giant) < 2:
        giant = set()
    small = sorted((c for c in comps if c is not giant and len(c) >= 2), key=len, reverse=True)
    singles = sorted(n for c in comps if c is not giant and len(c) == 1 for n in c)

    pos: dict[str, tuple[float, float]] = {}
    if giant:
        # G.subgraph() builds its view based on the set inside, so node order
        # there still depends on hash randomization even if giant is
        # pre-sorted - copy into a fresh graph with explicit order instead.
        sub = nx.Graph()
        sub.add_nodes_from(sorted(giant))
        sub.add_weighted_edges_from((a, b, d["weight"]) for a, b, d in G.edges(data=True) if a in giant and b in giant)
        pos = fit_coords(nx.forceatlas2_layout(sub, max_iter=max_iter, weight="weight", seed=seed))  # type: ignore[arg-type]

    rng = random.Random(seed + 1)
    crowd = list(pos.values()) or [(500.0, 500.0)]
    occupied: set[tuple[int, int]] = set()
    cell = STRANDED_MIN_SEP

    def place(x: float, y: float) -> tuple[float, float]:
        x = min(COORD_MAX, max(COORD_MIN, x))
        y = min(COORD_MAX, max(COORD_MIN, y))
        occupied.add((int(x // cell), int(y // cell)))
        return round(x, 1), round(y, 1)

    def free_spot(gen: Callable[[], tuple[float, float]]) -> tuple[float, float]:
        # range(60) is never empty, x/y always get reassigned - but static
        # analysis doesn't know that, hence the placeholder initial value,
        # which never actually reaches place().
        x, y = 0.0, 0.0
        for _attempt in range(60):
            x, y = gen()
            x = min(COORD_MAX, max(COORD_MIN, x))
            y = min(COORD_MAX, max(COORD_MIN, y))
            if (int(x // cell), int(y // cell)) not in occupied:
                break
        return place(x, y)

    for comp in small:
        ax, ay = rng.choice(crowd)
        ccx = min(940.0, max(60.0, rng.gauss(ax, STRANDED_JITTER)))
        ccy = min(940.0, max(60.0, rng.gauss(ay, STRANDED_JITTER)))
        radius = 6.0 + 2.2 * math.sqrt(len(comp))
        sx, sy = rng.uniform(0.55, 1.6), rng.uniform(0.55, 1.6)  # stretch/rotation, so patches aren't perfect circles
        ang = rng.uniform(0.0, math.pi)
        cos_a, sin_a = math.cos(ang), math.sin(ang)

        def patch_point(
            radius: float = radius,
            sx: float = sx,
            sy: float = sy,
            ccx: float = ccx,
            ccy: float = ccy,
            cos_a: float = cos_a,
            sin_a: float = sin_a,
        ) -> tuple[float, float]:
            dx, dy = rng.gauss(0, radius * sx), rng.gauss(0, radius * sy)
            return ccx + dx * cos_a - dy * sin_a, ccy + dx * sin_a + dy * cos_a

        for n in sorted(comp):
            pos[n] = free_spot(patch_point)

    for n in singles:

        def gen(ax: float = 0, ay: float = 0) -> tuple[float, float]:
            ax, ay = rng.choice(crowd)
            return rng.gauss(ax, STRANDED_JITTER), rng.gauss(ay, STRANDED_JITTER)

        pos[n] = free_spot(gen)

    stats = (len(giant), G.subgraph(giant).number_of_edges(), len(small), len(singles))
    return pos, stats


class ForceAtlasLayouter:
    """ForceAtlas2 layout - holds `seed` as state instead of a parameter on
    every individual call (otherwise it threads unchanged through the whole
    call chain in `GraphLayoutBuilder`). Two methods, matching the two usage
    patterns that actually exist in this project: `blended()` for
    authors/publications (blending small components + spreading
    collisions), `simple()` for repositories (plain FA2, neither of those -
    the repository graph is usually sparse enough not to need them).
    """

    def __init__(self, seed: int) -> None:
        self.seed = seed

    def blended(
        self, edge_weights: dict[tuple[str, str], float], all_ids: Iterable[str], max_iter: int, min_sep: float
    ) -> tuple[dict[str, tuple[float, float]], tuple[int, int, int, int]]:
        """FA2 with disconnected components blended in, plus collision spreading."""
        pos, stats = fa2_blended_layout(edge_weights, all_ids, max_iter, self.seed)
        return spread_min_distance(pos, min_sep, self.seed), stats

    def simple(self, graph: nx.Graph, max_iter: int) -> dict[str, tuple[float, float]]:
        """Plain FA2, no blending/spreading - the graph is already connected
        or sparse enough not to need either."""
        return fit_coords(nx.forceatlas2_layout(graph, max_iter=max_iter, weight="weight", seed=self.seed))  # type: ignore[arg-type]


@dataclass(frozen=True)
class Layout:
    """Node positions after layout, plus the edge weights that actually go
    to export (not to be confused with the weights used only to COMPUTE the
    layout - those are wider: `coauth`/`pub_pair_w` here are narrower than
    what FA2 sees, because layout additionally accounts for shared
    repositories and synthetic "same department" edges, while the "shared
    publications"/"shared authors" card in the UI must only show real
    connections)."""

    pos_authors: dict[str, tuple[float, float]]
    """Author key -> (x, y) coordinates in frontend space."""
    pos_pubs: dict[str, tuple[float, float]]
    """Publication key -> (x, y) coordinates."""
    pos_repos: dict[str, tuple[float, float]]
    """Repository key -> (x, y) coordinates."""
    coauth: dict[tuple[str, str], int]
    """Author pairs -> number of shared publications (real, for coauth_edges)."""
    pub_pair_w: dict[tuple[str, str], int]
    """Publication pairs -> number of shared ITMO authors (real, for pub_edges)."""
    repo_edge_w: dict[tuple[str, str], int]
    """Repository pairs -> number of shared publications (used for both layout and repo_edges - no split here)."""


class GraphLayoutBuilder:
    """Computes the three ForceAtlas2 layouts (authors/publications/
    repositories) - each with its own closeness measure, see
    `docs/architecture/gui.md`. Holds `db`/`authorship`/`assignment` as
    state so `build()` doesn't thread them as parameters - they're the same
    across all three layouts within one call.
    """

    def __init__(self, db: dict[str, list[dict]], authorship: Authorship, assignment: DepartmentAssignment) -> None:
        self.db = db
        self.authorship = authorship
        self.assignment = assignment

    def build(self, seed: int) -> Layout:
        """Computes the three ForceAtlas2 layouts (authors/publications/
        repositories) - each with its own closeness measure, see
        `docs/architecture/gui.md`.

        Args:
            seed: ForceAtlas2 seed and seed for blending disconnected components.

        Returns:
            `Layout` with positions and export edge weights.
        """
        rng = random.Random(seed)  # one shared generator for sparse_dept_edges (authors and publications), see the seed+1 note above
        layouter = ForceAtlasLayouter(seed)

        # --- authors: shared publications + shared repositories + sparse
        # "same department" edges. The first (coauth) goes both to layout and
        # to export as-is; repositories and department edges only affect layout.
        # Each pair of coauthors on one publication is a real edge, weighted
        # by how many publications they wrote together.
        coauth: dict[tuple[str, str], int] = defaultdict(int)
        for _pid, pers in self.authorship.pub_authors.items():
            for a, b in combinations(sorted(set(pers)), 2):
                coauth[(a, b)] += 1

        # A plain dict, not a Counter: fractional department-edge weights
        # (sparse_dept_edges) get blended into it below, and Counter is typed
        # as int-only in typeshed. dict(coauth) copies the real weights as a
        # starting point - everything after this only ADDS synthetic weight on top.
        author_layout_w: dict[tuple[str, str], float] = dict(coauth)
        # Who worked together on the same repository (CONTRIBUTED_TO) is also
        # reason to pull nodes closer on the map, even though it never
        # reaches coauth_edges (export) - only affects layout.
        repo_contributors: dict[str, set[str]] = defaultdict(set)
        for row in self.db["repo_persons"]:
            repo_contributors[row["rid"]].add(row["per"])
        for pers in repo_contributors.values():
            for a, b in combinations(sorted(pers), 2):
                author_layout_w[(a, b)] = author_layout_w.get((a, b), 0) + 1
        # Synthetic weak "same department" edges - see sparse_dept_edges,
        # only so colleagues with zero real connections don't scatter across the map.
        for pair, w in sparse_dept_edges(set(self.assignment.static_depts), self.assignment.author_dept, rng).items():
            author_layout_w[pair] = author_layout_w.get(pair, 0) + w

        t0 = time.time()
        pos_authors, (n_giant, e_giant, n_small, n_single) = layouter.blended(
            author_layout_w, set(self.assignment.static_depts), FA2_ITERATIONS.authors, MIN_SEPARATION.authors
        )
        logger.info(
            "FA2 authors: giant %d nodes / %d edges, blended in: %d small components + %d singletons, "
            "min-sep %.1f, %.1f s",
            n_giant, e_giant, n_small, n_single, MIN_SEPARATION.authors, time.time() - t0,
        )

        # --- publications: shared ITMO authors. The full w>=1 graph is
        # thousands of edges, so layout uses only the top-K strongest links
        # per publication; export (pub_edges) gets the full pub_pair_w, unclipped.
        # A pair of publications by the same author is an edge, weighted by
        # how many authors they share.
        pub_pair_w: dict[tuple[str, str], int] = defaultdict(int)
        for _per, plist in self.authorship.author_pubs.items():
            for a, b in combinations(sorted(set(plist)), 2):
                pub_pair_w[(a, b)] += 1

        t0 = time.time()
        # For each publication, collect its neighbors with edge weights -
        # from BOTH ends of the edge (a sees b, b sees a), so the top-K
        # strongest can be picked fairly for EACH publication separately,
        # not just a single top-K over the whole graph at once.
        strongest: dict[str, list[tuple[int, str]]] = defaultdict(list)
        for (a, b), w in pub_pair_w.items():
            strongest[a].append((w, b))
            strongest[b].append((w, a))
        pub_layout_w: dict[tuple[str, str], float] = {}
        for n, lst in strongest.items():
            lst.sort(key=lambda t: (-t[0], t[1]))  # strongest first, ties by neighbor id for determinism
            for w, o in lst[: EDGE_THRESHOLDS.pub_layout_top_k]:
                pub_layout_w[(n, o) if n < o else (o, n)] = w  # key always (smaller, larger) - never duplicate an edge
        # The same "same department" synthetic edges as for authors above,
        # just with weaker parameters (see pub_dept_edge_k/weight) and a
        # taper_size - publications can have far more entities per department.
        for pair, w in sparse_dept_edges(
            self.authorship.pub_ids,
            self.assignment.pub_primary,
            rng,
            k=SYNTHETIC_DEPT_EDGES.pub_dept_edge_k,
            weight=SYNTHETIC_DEPT_EDGES.pub_dept_edge_weight,
            taper_size=150,
        ).items():
            pub_layout_w[pair] = pub_layout_w.get(pair, 0) + w

        pos_pubs, (n_giant_p, e_giant_p, n_small_p, n_single_p) = layouter.blended(
            pub_layout_w, self.authorship.pub_ids, FA2_ITERATIONS.pubs, MIN_SEPARATION.pubs
        )
        logger.info(
            "FA2 publications: giant %d nodes / %d edges, blended in: %d small components + %d singletons, "
            "min-sep %.1f, %.1f s",
            n_giant_p, e_giant_p, n_small_p, n_single_p, MIN_SEPARATION.pubs, time.time() - t0,
        )

        # --- repositories: shared publications (including publications
        # outside the graph, with zero ITMO authors - a repository still
        # implements them regardless, hence db["repo_pubs"] as a whole here,
        # not authorship.pub_ids).
        repo_all_pubs: dict[str, set[str]] = defaultdict(set)
        for row in self.db["repo_pubs"]:
            repo_all_pubs[row["rid"]].add(row["pid"])
        # Edge weight is simply the number of publications a pair of
        # repositories share (set intersection); zero shared publications
        # means no edge at all.
        repo_edge_w: dict[tuple[str, str], int] = {}
        for a, b in combinations(sorted(repo_all_pubs), 2):
            shared = len(repo_all_pubs[a] & repo_all_pubs[b])
            if shared:
                repo_edge_w[(a, b)] = shared

        # A plain graph, no blending/spreading (see ForceAtlasLayouter.simple
        # docstring) - an order of magnitude fewer repositories than
        # authors/publications, sparse graph.
        R = nx.Graph()
        R.add_nodes_from(r["id"] for r in self.db["repositories"])
        R.add_weighted_edges_from((a, b, w) for (a, b), w in repo_edge_w.items())
        pos_repos = layouter.simple(R, FA2_ITERATIONS.repos)

        # coauth/pub_pair_w/repo_edge_w are the REAL weights - go both to
        # layout (via author_layout_w/pub_layout_w above) and to export as-is
        # (see the Layout docstring for why these are different things).
        return Layout(
            pos_authors=pos_authors,
            pos_pubs=pos_pubs,
            pos_repos=pos_repos,
            coauth=dict(coauth),
            pub_pair_w=dict(pub_pair_w),
            repo_edge_w=repo_edge_w,
        )
