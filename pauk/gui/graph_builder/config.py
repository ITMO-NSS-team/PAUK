"""Tuning constants for pauk/gui, grouped by which part of the
layout/build they configure - the group name says what a value is for
without needing to read further.

Plain frozen dataclasses, no env var overrides - nobody tunes ForceAtlas2
iteration counts through the environment, easier to just edit the number here.

Constants needed by exactly one function, with no variation between calls
(e.g. the frontend coordinate space, jitter sigma when blending stranded
nodes) live locally next to that function in layout.py instead.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EdgeThresholds:
    """Minimum weights below which an edge is dropped from the export entirely."""

    coauth_min_w: int = 2
    """Min. shared publications for an author-author edge."""
    pub_edge_min_w: int = 3
    """Min. shared authors for a publication-publication edge."""
    pub_layout_top_k: int = 6
    """How many of a publication's strongest "shared author" edges to keep for layout (not for export)."""
    hyperauthor_pub_size: int = 50
    """Above this many authors a publication only links its ITMO authors to everyone, not all pairs (see `layout.py::coauthor_pairs`)."""


EDGE_THRESHOLDS = EdgeThresholds()


@dataclass(frozen=True)
class Fa2Iterations:
    """ForceAtlas2 iteration count, one run per entity type."""

    authors: int = 300
    """Authors are usually the most numerous with the densest graph (coauthorship + shared repos) - needs more iterations to converge."""
    pubs: int = 250
    """Also many publications, but layout only uses the top-K strongest links per publication - converges a bit faster."""
    repos: int = 100
    """An order of magnitude fewer repositories, sparse graph - converges quickly without many iterations."""


FA2_ITERATIONS = Fa2Iterations()


@dataclass(frozen=True)
class SyntheticDeptEdges:
    """Weak synthetic "same department" edges - not real connections, a hint
    for layout so a department doesn't sprawl into a shapeless cloud (see
    `layout.py::sparse_dept_edges`)."""

    dept_edge_k: int = 3
    """How many random department colleagues each node (author) connects to."""
    dept_edge_weight: float = 1.0
    """Comparable to real edges (shared publications start at 1.0)."""
    pub_dept_edge_k: int = 1
    """Same idea as `dept_edge_k`, for publications."""
    pub_dept_edge_weight: float = 0.5
    """Weaker than `dept_edge_weight` - publications already have plenty of real edges."""
    repo_dept_edge_k: int = 2
    """Same idea as `dept_edge_k`, for repositories."""
    repo_dept_edge_weight: float = 0.5
    """Weaker than real repository edges, only holds a department together."""


SYNTHETIC_DEPT_EDGES = SyntheticDeptEdges()


@dataclass(frozen=True)
class MinSeparation:
    """Minimum distance between nodes - a pass after layout itself
    (`layout.py::spread_min_distance`), so coincident points don't clump
    into a solid blob."""

    authors: float = 5.0
    """Authors are usually far more numerous than publications on the map - points need more breathing room to stay visually distinct."""
    pubs: float = 4.0
    """Fewer publications than authors, so less separation is needed."""
    repos: float = 6.0
    """Repository icons are the largest of the three, so they need the most room."""


MIN_SEPARATION = MinSeparation()


@dataclass(frozen=True)
class RepoEdges:
    """Repository-repository edges: one weight per signal, ordered by how
    much the signal says about the code itself (see `layout.py::repo_edge_signals`)."""

    w_pub: float = 3.0
    """Both implement the same publication."""
    w_person: float = 2.0
    """The same ITMO person contributed to both."""
    w_coauthor: float = 1.0
    """Their publications share an ITMO author."""
    w_owner: float = 1.0
    """The same GitHub account owns both - a lab account holds both its flagship and its coursework."""
    group_cap: int = 60
    """Groups larger than this are noise, not signal (a bot credited on two hundred repositories)."""
    top_k: int = 12
    """Strongest edges kept per repository, otherwise dense groups render as solid ink."""
    group_min: int = 2
    """Smallest org/field group worth a colour of its own (see `departments.py::repo_groups`)."""


REPO_EDGES = RepoEdges()


# --- Display placeholders ------------------------------------------------------
# A trio for the same synthetic bucket "publication/author/repository with no
# known department" (see departments.py::DepartmentAssigner) - not a real
# department, so it doesn't go through golden_color() (departments.py) and
# gets its own neutral gray, unmistakable for a real one.
NO_DEPT_NAME = "Без департамента"
NO_DEPT_NAME_EN = "No department"
NO_DEPT_COLOR = "#8a8f98"
