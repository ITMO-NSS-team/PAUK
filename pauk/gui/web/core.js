"use strict";

// ---------- projection & zoom constants ----------

const S    = 1000;
const SPAN = 16;
const proj = (x, y) => [(x / S - 0.5) * SPAN, (0.5 - y / S) * SPAN];
const FIT  = [[-7.7, -7.7], [7.7, 7.7]];

const DATA = window.GRAPH;

const DEPT_EDGE_OPACITY  = ["interpolate", ["linear"], ["zoom"], 4, 0.45, 6.8, 0.45, 7.6, 0];
const NODE_OPACITY       = ["interpolate", ["linear"], ["zoom"], 4.5, 0, 6.0, 1];
const EDGE_OPACITY_COAUTH = ["interpolate", ["linear"], ["zoom"], 5.6, 0, 7.5, 0.28, 12, 0.55];
const EDGE_OPACITY_PUBS   = ["interpolate", ["linear"], ["zoom"], 5.6, 0, 7.5, 0.28, 12, 0.55];
const FILL_OPACITY       = 0.35;
const AUTHOR_LABEL_ZOOM  = 8.8;
// Below this zoom, clicks resolve to the department under the cursor —
// nodes are too tightly packed to aim for individually.
const DEPT_CLICK_ZOOM = 6.8;

const nodeByKey = new Map();
DATA.authors.forEach(n => nodeByKey.set(n.key, n));
DATA.repos.forEach(n => nodeByKey.set(n.key, n));
DATA.pubs.forEach(n => nodeByKey.set(n.key, n));

// ---------- color ----------

// Desaturates generate_data.py's fully-saturated colors to match the grayscale chrome.
const NODE_COLOR_SAT_MUL = 0.55;
function hexToHsl(hex) {
  const r = parseInt(hex.slice(1, 3), 16) / 255, g = parseInt(hex.slice(3, 5), 16) / 255, b = parseInt(hex.slice(5, 7), 16) / 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b);
  let h = 0, s = 0; const l = (max + min) / 2;
  if (max !== min) {
    const d = max - min;
    s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
    if (max === r) h = (g - b) / d + (g < b ? 6 : 0);
    else if (max === g) h = (b - r) / d + 2;
    else h = (r - g) / d + 4;
    h /= 6;
  }
  return [h, s, l];
}
function hslToHex(h, s, l) {
  const hue2rgb = (p, q, t) => {
    if (t < 0) t += 1; if (t > 1) t -= 1;
    if (t < 1 / 6) return p + (q - p) * 6 * t;
    if (t < 1 / 2) return q;
    if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6;
    return p;
  };
  let r, g, b;
  if (s === 0) { r = g = b = l; }
  else {
    const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
    const p = 2 * l - q;
    r = hue2rgb(p, q, h + 1 / 3); g = hue2rgb(p, q, h); b = hue2rgb(p, q, h - 1 / 3);
  }
  const toHex = v => Math.round(v * 255).toString(16).padStart(2, "0");
  return `#${toHex(r)}${toHex(g)}${toHex(b)}`;
}
function muteColor(hex) {
  const [h, s, l] = hexToHsl(hex);
  return hslToHex(h, s * NODE_COLOR_SAT_MUL, l);
}

const deptById = new Map();
DATA.departments.forEach(d => { d.color = muteColor(d.color); deptById.set(d.id, d); });

// Canonical per-kind accent color, shown on the Overview panel's tab label
// and reused anywhere a kind badge appears (search results). Department has
// no tab of its own, so it gets full-strength text instead of a hue — plain
// var(--muted) read as washed-out next to the three saturated colors.
const KIND_COLOR = { author: "var(--accent-2)", repo: "#d9962e", pub: "#e8562e", dept: "var(--text)" };

// ---------- display names ----------
// d.name / n.label are the data of record — use these two only for what's
// shown to the user, never for comparisons, keys, or sorting.

function deptDisplayName(d) {
  if (!d) return "";
  return (LANG === "en" && d.name_en) ? d.name_en : d.name;
}

function authorDisplayName(n) {
  if (!n) return "";
  return (LANG === "en" && n.name_en) ? n.name_en : n.label;
}

// ---------- adjacency indices ----------

const authorPubs = new Map();
const pubAuthors = new Map();
DATA.all_edges.forEach(({s, t}) => {
  if (!authorPubs.has(s)) authorPubs.set(s, []);
  authorPubs.get(s).push(t);
  if (!pubAuthors.has(t)) pubAuthors.set(t, []);
  pubAuthors.get(t).push(s);
});

function buildAdjIndex(edges) {
  const idx = new Map();
  const push = (from, to, w) => { if (!idx.has(from)) idx.set(from, []); idx.get(from).push({ o: to, w }); };
  edges.forEach(e => { const w = e.w || 1; push(e.s, e.t, w); push(e.t, e.s, w); });
  return idx;
}
const coauthAdj = buildAdjIndex(DATA.coauth_edges);
const pubAdj    = buildAdjIndex(DATA.pub_edges);
const repoAdj   = buildAdjIndex(DATA.repo_edges);

const repoPubs = new Map();
const pubRepos = new Map();
(DATA.repo_pub_edges || []).forEach(e => {
  if (!repoPubs.has(e.s)) repoPubs.set(e.s, []);
  repoPubs.get(e.s).push(e.t);
  if (!pubRepos.has(e.t)) pubRepos.set(e.t, []);
  pubRepos.get(e.t).push(e.s);
});

const authorRepos = new Map();
DATA.repo_author_edges.forEach(e => {
  if (!authorRepos.has(e.t)) authorRepos.set(e.t, new Set());
  authorRepos.get(e.t).add(e.s);
});
authorPubs.forEach((pubKeys, authorKey) => {
  pubKeys.forEach(pk => {
    (pubRepos.get(pk) || []).forEach(rk => {
      if (!authorRepos.has(authorKey)) authorRepos.set(authorKey, new Set());
      authorRepos.get(authorKey).add(rk);
    });
  });
});
authorRepos.forEach((set, key) => authorRepos.set(key, [...set]));

const repoPersons = new Map();
DATA.repo_author_edges.forEach(e => {
  if (!repoPersons.has(e.s)) repoPersons.set(e.s, []);
  repoPersons.get(e.s).push({ key: e.t, role: e.role });
});

// ---------- tab accessors ----------

function tabNodes() {
  switch (tab) {
    case 1: return DATA.authors;
    case 2: return DATA.repos;
    case 3: return DATA.pubs;
    default: return [];
  }
}

function tabEdges() {
  switch (tab) {
    case 1: return DATA.coauth_edges;
    case 2: return DATA.repo_edges;
    case 3: return DATA.pub_edges;
    default: return [];
  }
}

function P(key) {
  const n = nodeByKey.get(key);
  if (!n) return [500, 500];
  return [n.gx ?? 500, n.gy ?? 500];
}

const nodeColor = n => deptById.get(n.dept)?.color || "#9aa2ac";

// ---------- sizing ----------

// Co-authors of an author: Map<key, total weight>
function coauthMapOf(key) {
  const m = new Map();
  (coauthAdj.get(key) || []).forEach(({ o, w }) => m.set(o, (m.get(o) || 0) + w));
  return m;
}

function szOf(n) {
  if (n.kind === "repo") return 0.6 + Math.min(2.4, 0.6 * Math.log1p((n.stars || 0) / 3));
  const c = n.kind === "author" ? (n.pubs_count || 0) : (n.n_authors || 0);
  return 0.6 + Math.min(1.5, 0.45 * Math.log1p(c));
}

// Shared by main.js (icon-size expressions) and overlay.js (label offset).
// `unit` = screen px per icon-size 1.0 — smaller than the raster's logical
// size since circleImg/squareImg reserve padding for their drop shadow.
const NODE_ICON_K = {
  author: { z3: 0.114, z9: 0.714, unit: 14 },
  repo:   { z3: 0.214, z9: 1.0,   unit: 14 },
  pub:    { z3: 0.18,  z9: 1.1,   unit: 7 },
};
function nodeScreenDiameter(n, zoom) {
  const k = NODE_ICON_K[n.kind]; if (!k) return 10;
  const t = Math.max(0, Math.min(1, (zoom - 3) / (9 - 3)));
  return (k.z3 + (k.z9 - k.z3) * t) * szOf(n) * k.unit;
}

// On-screen px for the sel-points marker (selection.js), shared with overlay.js.
const SEL_MARKER_PX = { focus: 22, n: 14 };

// ---------- misc utils ----------

const shortLabel = (s, n = 80) => !s || s.length <= n ? (s || "…") : s.slice(0, n - 1) + "…";

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function empty() { return { type: "FeatureCollection", features: [] }; }

// ---------- publication detail merge (from graph-search.js) ----------

window._pubDetailReady = false;
window._onDetailReady = function(detail) {
  detail.forEach(d => {
    const n = nodeByKey.get(d.key);
    if (!n) return;
    n.label    = d.label;
    n.journal  = d.journal;
    n.doi      = d.doi;
    n.has_code = d.has_code;
    n.code_url = d.code_url;
  });
  window._pubDetailReady = true;
  document.querySelectorAll(".search-loading-hint").forEach(el => el.classList.add("hidden"));
  if (typeof window._rebuildPubSearch === "function") window._rebuildPubSearch();
  // refresh the current search-page view so deep-linked profiles get backfilled
  if (tab === 4 && typeof _spRefreshCurrentView === "function") _spRefreshCurrentView();
};
if (window._pendingDetail) {
  window._onDetailReady(window._pendingDetail);
  delete window._pendingDetail;
}
