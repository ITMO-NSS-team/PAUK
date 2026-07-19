"use strict";

var selected     = null;
var selectedDept = null;

function nodeNeighbors(key) {
  const n = nodeByKey.get(key);
  if (!n) return [];
  if (n.kind === "author") {
    if (tab === 1) {
      const coauths = [];
      DATA.coauth_edges.forEach(e => {
        if (e.s === key) coauths.push(e.t);
        else if (e.t === key) coauths.push(e.s);
      });
      return coauths;
    }
    if (tab === 2) return authorRepos.get(key) || [];
    return authorPubs.get(key) || [];
  }
  if (n.kind === "pub") {
    if (tab === 3) {
      const related = [];
      DATA.pub_edges.forEach(e => {
        if (e.s === key) related.push(e.t);
        else if (e.t === key) related.push(e.s);
      });
      return related;
    }
    return pubAuthors.get(key) || [];
  }
  if (n.kind === "repo") {
    if (tab === 2) {
      const linked = [];
      DATA.repo_edges.forEach(e => {
        if (e.s === key) linked.push(e.t);
        else if (e.t === key) linked.push(e.s);
      });
      return linked;
    }
    return (repoPersons.get(key) || []).map(p => p.key);
  }
  return [];
}

function selectNode(key) {
  resetDeptFocus();
  selected = key;
  const n = nodeByKey.get(key);
  if (!n) return;
  const neigh = nodeNeighbors(key);
  const fp = P(key);
  map.getSource("sel-edges").setData({
    type: "FeatureCollection",
    features: neigh.map(nk => {
      const p = P(nk);
      return { type: "Feature", properties: {},
        geometry: { type: "LineString", coordinates: [proj(fp[0], fp[1]), proj(p[0], p[1])] } };
    }),
  });
  const pts = [{ key, role: "focus" }, ...neigh.map(nk => ({ key: nk, role: "n" }))];
  map.getSource("sel-points").setData({
    type: "FeatureCollection",
    features: pts.map(o => {
      const p = P(o.key), nd = nodeByKey.get(o.key);
      return { type: "Feature",
        geometry: { type: "Point", coordinates: proj(p[0], p[1]) },
        properties: { role: o.role, color: nd ? nodeColor(nd) : "#9aa2ac" } };
    }),
  });
  map.setPaintProperty("authors",   "circle-opacity", 0.12);
  map.setPaintProperty("repos",     "circle-opacity", 0.12);
  map.setPaintProperty("pubs",      "icon-opacity",   0.12);
  map.setPaintProperty("dept-fill", "fill-opacity",   0.06);
  showNodeCard(key);
  drawOverlay();
}

function selectEdge(props) {
  resetDeptFocus();
  selected = null;
  map.getSource("sel-edges").setData(empty());
  map.getSource("sel-points").setData(empty());
  showEdgeCard(props);
}

function clearAll() {
  selected = null; selectedDept = null;
  map.getSource("sel-edges").setData(empty());
  map.getSource("sel-points").setData(empty());
  map.getSource("dept-focus").setData(empty());
  if (tab === 1) {
    map.setPaintProperty("dept-edges", "line-opacity", DEPT_EDGE_OPACITY);
    map.setPaintProperty("dept-fill",  "fill-opacity", FILL_OPACITY);
  }
  map.setPaintProperty("authors",   "circle-opacity", NODE_OPACITY);
  map.setPaintProperty("repos",     "circle-opacity", tab === 2 ? 1 : NODE_OPACITY);
  map.setPaintProperty("pubs",      "icon-opacity",   NODE_OPACITY);
  map.setPaintProperty("sel-edges", "line-width",     1.8);
  closePanel();
  drawOverlay();
}
