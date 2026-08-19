"use strict";

var selected     = null;
var selectedDept = null;

function nodeNeighbors(key) {
  const n = nodeByKey.get(key);
  if (!n) return [];
  if (n.kind === "author") {
    if (tab === 1) return (coauthAdj.get(key) || []).map(o => o.o);
    if (tab === 2) return authorRepos.get(key) || [];
    return authorPubs.get(key) || [];
  }
  if (n.kind === "pub") {
    if (tab === 3) return (pubAdj.get(key) || []).map(o => o.o);
    return pubAuthors.get(key) || [];
  }
  if (n.kind === "repo") {
    if (tab === 2) return (repoAdj.get(key) || []).map(o => o.o);
    return (repoPersons.get(key) || []).map(p => p.key);
  }
  return [];
}

// Icon-size multiplier that gets a sel-points marker to SEL_MARKER_PX[role] on screen.
function _selIconSize(kind, role) {
  const unit = kind === "pub" ? NODE_ICON_K.pub.unit : NODE_ICON_K.author.unit;
  return SEL_MARKER_PX[role] / unit;
}

function selectNode(key) {
  resetDeptFocus();
  selected = key;
  const n = nodeByKey.get(key);
  if (!n) return;
  const neigh = nodeNeighbors(key);
  const fp = P(key);
  map.flyTo({ center: proj(fp[0], fp[1]), zoom: Math.max(map.getZoom(), 6.5), duration: 700 });
  map.setPaintProperty("sel-edges", "line-color", nodeColor(n));
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
      const color = nd ? nodeColor(nd) : "#9aa2ac";
      const isPub = nd?.kind === "pub";
      return { type: "Feature",
        geometry: { type: "Point", coordinates: proj(p[0], p[1]) },
        properties: {
          iid: isPub ? ensureSquareImage(color) : ensureCircleImage(color),
          isz: _selIconSize(nd?.kind, o.role),
        } };
    }),
  });
  map.setPaintProperty("authors", "icon-opacity", 0.12);
  map.setPaintProperty("repos",   "icon-opacity", 0.12);
  map.setPaintProperty("pubs",    "icon-opacity", 0.12);
  map.setPaintProperty("dept-fill", "fill-opacity", 0.06);
  map.setPaintProperty("edges", "line-opacity", 0);
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
  map.setPaintProperty("authors", "icon-opacity", NODE_OPACITY);
  map.setPaintProperty("repos",   "icon-opacity", tab === 2 ? 1 : NODE_OPACITY);
  map.setPaintProperty("pubs",    "icon-opacity", NODE_OPACITY);
  map.setPaintProperty("edges", "line-opacity", tab === 2 ? 1 : tab === 3 ? EDGE_OPACITY_PUBS : EDGE_OPACITY_COAUTH);
  closePanel();
  drawOverlay();
}