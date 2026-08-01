"use strict";

const S    = 1000;
const SPAN = 16;
const proj = (x, y) => [(x / S - 0.5) * SPAN, (0.5 - y / S) * SPAN];
const FIT  = [[-7.7, -7.7], [7.7, 7.7]];

const DATA = window.GRAPH;

const DEPT_EDGE_OPACITY  = ["interpolate", ["linear"], ["zoom"], 4, 0.45, 6.8, 0.45, 7.6, 0];
const FOCUS_EDGE_OPACITY = ["interpolate", ["linear"], ["zoom"], 4, 0.85, 6.8, 0.85, 7.6, 0];
const NODE_OPACITY       = ["interpolate", ["linear"], ["zoom"], 4.5, 0, 6.0, 1];
const EDGE_OPACITY_COAUTH = ["interpolate", ["linear"], ["zoom"], 5.6, 0, 7.5, 0.28, 12, 0.55];
const EDGE_OPACITY_PUBS   = ["interpolate", ["linear"], ["zoom"], 5.6, 0, 7.5, 0.28, 12, 0.55];
const FILL_OPACITY       = 0.35;
const AUTHOR_LABEL_ZOOM  = 8.8;

const nodeByKey = new Map();
DATA.authors.forEach(n => nodeByKey.set(n.key, n));
DATA.repos.forEach(n => nodeByKey.set(n.key, n));
DATA.pubs.forEach(n => nodeByKey.set(n.key, n));

// Desaturate the fully-saturated colors from generate_data.py to match the grayscale chrome
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

const authorPubs = new Map();
const pubAuthors = new Map();
DATA.all_edges.forEach(({s, t}) => {
  if (!authorPubs.has(s)) authorPubs.set(s, []);
  authorPubs.get(s).push(t);
  if (!pubAuthors.has(t)) pubAuthors.set(t, []);
  pubAuthors.get(t).push(s);
});

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

// Co-authors of an author: Map<key, total weight>
function coauthMapOf(key) {
  const m = new Map();
  DATA.coauth_edges.forEach(e => {
    if (e.s !== key && e.t !== key) return;
    const ok = e.s === key ? e.t : e.s;
    m.set(ok, (m.get(ok) || 0) + (e.w || 1));
  });
  return m;
}

function szOf(n) {
  if (n.kind === "repo") return 0.6 + Math.min(2.4, 0.6 * Math.log1p((n.stars || 0) / 3));
  const c = n.kind === "author" ? (n.pubs_count || 0) : (n.n_authors || 0);
  return 0.6 + Math.min(1.5, 0.45 * Math.log1p(c));
}

const shortLabel = (s, n = 80) => !s || s.length <= n ? (s || "…") : s.slice(0, n - 1) + "…";

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function empty() { return { type: "FeatureCollection", features: [] }; }

// Merge publication text data loaded from graph-search.js
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