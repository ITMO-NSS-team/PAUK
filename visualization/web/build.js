"use strict";

const MIN_HULL_NODES = 30;
const BACKBONE_N     = 35;
// Members closer than this (data units, 0..1000) count as one spatial island.
// A department's hull is drawn around its LARGEST island only: members are often
// scattered across several collaboration clusters, and a hull over all of them
// would span half the map and overlap other territories. Counts shown in the UI
// still reflect all members — this only affects the drawn shape.
const HULL_LINK_DIST = 30;

// Convex polygon clip (Sutherland–Hodgman). Both rings are [[x,y],...]; the
// clip ring must be convex (Voronoi cells are). Returns possibly-empty ring.
function clipConvex(subject, clip) {
  const cross = (a, b, p) => (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]);
  // normalize clip to counter-clockwise so "inside" is cross >= 0
  let area = 0;
  for (let i = 0; i < clip.length; i++) {
    const p = clip[i], q = clip[(i + 1) % clip.length];
    area += p[0] * q[1] - q[0] * p[1];
  }
  if (area < 0) clip = [...clip].reverse();
  let output = subject;
  for (let i = 0; i < clip.length && output.length; i++) {
    const A = clip[i], B = clip[(i + 1) % clip.length];
    const input = output;
    output = [];
    for (let j = 0; j < input.length; j++) {
      const P = input[j], Q = input[(j + 1) % input.length];
      const inP = cross(A, B, P) >= 0, inQ = cross(A, B, Q) >= 0;
      if (inP) output.push(P);
      if (inP !== inQ) {
        const t = cross(A, B, P) / (cross(A, B, P) - cross(A, B, Q));
        output.push([P[0] + t * (Q[0] - P[0]), P[1] + t * (Q[1] - P[1])]);
      }
    }
  }
  return output;
}

// Largest spatial island via grid-linkage: points in the same or adjacent
// grid cells (cell = HULL_LINK_DIST) belong to one island.
function dominantIsland(pts) {
  const cell = HULL_LINK_DIST;
  const grid = new Map();
  pts.forEach(p => {
    const k = Math.floor(p[0] / cell) + "," + Math.floor(p[1] / cell);
    if (!grid.has(k)) grid.set(k, []);
    grid.get(k).push(p);
  });
  const seen = new Set();
  let best = [];
  for (const start of grid.keys()) {
    if (seen.has(start)) continue;
    seen.add(start);
    const island = [];
    const stack = [start];
    while (stack.length) {
      const k = stack.pop();
      island.push(...grid.get(k));
      const [cx, cy] = k.split(",").map(Number);
      for (let dx = -1; dx <= 1; dx++) for (let dy = -1; dy <= 1; dy++) {
        const nk = (cx + dx) + "," + (cy + dy);
        if (grid.has(nk) && !seen.has(nk)) { seen.add(nk); stack.push(nk); }
      }
    }
    if (island.length > best.length) best = island;
  }
  return best;
}

var deptCentroid = new Map();

function buildHullFeatures() {
  const byDept = new Map();
  tabNodes().forEach(n => {
    if (!byDept.has(n.dept)) byDept.set(n.dept, []);
    byDept.get(n.dept).push(P(n.key));
  });

  deptCentroid = new Map();
  tabNodes().forEach(n => {
    if (!byDept.has(n.dept)) return;
    const [x, y] = P(n.key);
    if (!deptCentroid.has(n.dept)) deptCentroid.set(n.dept, [0, 0, 0]);
    const acc = deptCentroid.get(n.dept);
    acc[0] += x; acc[1] += y; acc[2]++;
  });
  deptCentroid.forEach((acc, did) => {
    deptCentroid.set(did, [acc[0] / acc[2], acc[1] / acc[2]]);
  });

  // Pass 1: dominant island + raw hull per eligible department
  const cands = [];
  byDept.forEach((pts, did) => {
    const d = deptById.get(did);
    if (!d || d.name === "Без департамента" || pts.length < MIN_HULL_NODES) return;
    const core = dominantIsland(pts);
    if (core.length < MIN_HULL_NODES) return;
    const hull = d3.polygonHull(core);
    if (!hull) return;
    const [cx, cy] = d3.polygonCentroid(hull);
    const inflated = hull.map(([x, y]) => [cx + (x - cx) * 1.08, cy + (y - cy) * 1.08]);
    cands.push({ did, d, n: pts.length, hull: inflated, cx, cy });
  });

  // Pass 2: clip each hull by the Voronoi cell of its island centroid —
  // Voronoi cells are disjoint, so territories can never overlap.
  const features = [];
  if (cands.length) {
    const voronoi = d3.Delaunay.from(cands.map(c => [c.cx, c.cy]))
      .voronoi([-100, -100, 1100, 1100]);
    cands.forEach((c, i) => {
      const cell = voronoi.cellPolygon(i);
      let ringPts = c.hull;
      if (cell && cell.length > 3) ringPts = clipConvex(c.hull, cell.slice(0, -1));
      if (ringPts.length < 3) return;
      const ring = [...ringPts.map(([x, y]) => proj(x, y)), proj(ringPts[0][0], ringPts[0][1])];
      features.push({
        type: "Feature",
        properties: { id: c.did, color: c.d.color, name: c.d.name, n: c.n },
        geometry: { type: "Polygon", coordinates: [ring] },
      });
    });
  }

  return { type: "FeatureCollection", features };
}

function buildBackboneFeatures() {
  const valid = (DATA.dept_edges || []).filter(
    e => deptCentroid.has(e.s) && deptCentroid.has(e.t)
  );
  return {
    type: "FeatureCollection",
    features: [...valid].sort((a, b) => b.w - a.w).slice(0, BACKBONE_N).map(e => {
      const [ax, ay] = deptCentroid.get(e.s), [bx, by] = deptCentroid.get(e.t);
      return {
        type: "Feature", properties: { w: e.w },
        geometry: { type: "LineString", coordinates: [proj(ax, ay), proj(bx, by)] },
      };
    }),
  };
}

function buildNodeFeatures() {
  return {
    type: "FeatureCollection",
    features: tabNodes().map(n => {
      const [x, y] = P(n.key);
      const color  = nodeColor(n);
      const props  = { key: n.key, kind: n.kind, color, dept: n.dept, sz: szOf(n) };
      if (n.kind === "pub") { props.sqid = ensureSquareImage(color); props.year = n.year || 0; }
      return { type: "Feature", properties: props, geometry: { type: "Point", coordinates: proj(x, y) } };
    }),
  };
}

function buildEdgeFeatures() {
  const features = [];
  for (const e of tabEdges()) {
    const sn = nodeByKey.get(e.s), tn = nodeByKey.get(e.t);
    if (!sn || !tn) continue;
    if (tab === 3 && (e.w || 1) < 3) continue;
    if (tab === 3 && (sn.year > yearMax || tn.year > yearMax)) continue;
    const [sx, sy] = P(e.s);
    const [tx, ty] = P(e.t);
    const min_rank = Math.min(sn.rank ?? 0, tn.rank ?? 0);
    features.push({
      type: "Feature",
      properties: { s: e.s, t: e.t, w: e.w || 1, kind: e.kind || "", min_rank },
      geometry: { type: "LineString", coordinates: [proj(sx, sy), proj(tx, ty)] },
    });
  }
  return { type: "FeatureCollection", features };
}
