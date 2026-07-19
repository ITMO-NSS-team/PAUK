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

const deptById = new Map();
DATA.departments.forEach(d => deptById.set(d.id, d));

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
  if (typeof window._rebuildPubSearch === "function") window._rebuildPubSearch();
  if (tab === 4 && typeof spShowLanding === "function" &&
      typeof spOnLanding !== "undefined" && spOnLanding) spShowLanding();
};
if (window._pendingDetail) {
  window._onDetailReady(window._pendingDetail);
  delete window._pendingDetail;
}
