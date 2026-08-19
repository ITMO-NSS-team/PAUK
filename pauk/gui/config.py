"""Tuning constants for pauk/gui/generate_data.py and pauk/gui/layout.py —
knobs that genuinely vary between author/pub/repo call sites. Plain
constants, no env var overrides — nobody tunes a ForceAtlas2 iteration
count via the environment, just edit the number here.

Constants used in exactly one function with no per-call variation (e.g.
the frontend coordinate space, the blend-in scatter sigma) live as local
constants next to that function in layout.py instead of here.
"""

# --- Display fallbacks ----------------------------------------------------------
NO_DEPT_NAME = "Без департамента"
NO_DEPT_NAME_EN = "No department"
NO_DEPT_COLOR = "#8a8f98"

# --- Edge thresholds ------------------------------------------------------------
COAUTH_MIN_W = 2  # min joint publications for an author-author edge
PUB_EDGE_MIN_W = 3  # min shared ITMO authors for a publication-publication edge
PUB_LAYOUT_TOP_K = 6  # strongest shared-author edges kept per publication for layout

# --- ForceAtlas2 iteration counts, one run per entity type ----------------------
FA2_ITER_AUTHORS = 300
FA2_ITER_PUBS = 250
FA2_ITER_REPOS = 100

# --- Synthetic "shared department" peer edges -----------------------------------
DEPT_EDGE_K = 3  # random same-department peers each node is tied to
DEPT_EDGE_WEIGHT = 1.0  # comparable to real edges (joint pubs start at 1.0)
PUB_DEPT_EDGE_K = 1
PUB_DEPT_EDGE_WEIGHT = 0.5

# --- Minimum node separation (collision pass after layout) ----------------------
MIN_SEP_AUTHORS = 4.5
MIN_SEP_PUBS = 3.5
