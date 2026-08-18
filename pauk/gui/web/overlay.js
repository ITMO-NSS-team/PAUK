"use strict";

const overlay = document.getElementById("overlay");
const octx    = overlay.getContext("2d");

// Label colors come from theme CSS variables; refreshed on theme toggle
var _labelHalo = "rgba(255,255,255,0.92)";
var _labelText = "#191F1D";
function refreshLabelColors() {
  const cs = getComputedStyle(document.documentElement);
  _labelHalo = cs.getPropertyValue("--label-halo").trim() || _labelHalo;
  _labelText = cs.getPropertyValue("--label-text").trim() || _labelText;
}
refreshLabelColors();

function sizeOverlay() {
  const dpr = window.devicePixelRatio || 1;
  overlay.width  = innerWidth  * dpr; overlay.height = innerHeight * dpr;
  overlay.style.width  = innerWidth  + "px";
  overlay.style.height = innerHeight + "px";
  octx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

window.addEventListener("resize", () => { sizeOverlay(); drawOverlay(); });

function wrap(text, max) {
  const words = text.split(" "), lines = []; let cur = "";
  for (const w of words) {
    if ((cur + " " + w).trim().length > max && cur) { lines.push(cur); cur = w; }
    else cur = (cur + " " + w).trim();
  }
  if (cur) lines.push(cur);
  return lines.slice(0, 3);
}

// Long labels (publication titles especially) wrap to 2-3 lines — a fixed
// clearance margin only accounts for one, so the bottom line kept dipping
// back into the node. `extra` grows the offset so the block's bottom edge,
// not its center, is what actually clears the icon.
function _labelMetrics(text, fs) {
  const lines = wrap(text, fs > 16 ? 16 : 22).length;
  const lh = fs * 1.1;
  return { lines, lh, extra: (lines - 1) * lh / 2 + fs / 2 };
}

function label(text, x, y, fs, color) {
  octx.font = `600 ${fs}px ${getComputedStyle(document.body).fontFamily}`;
  const lines = wrap(text, fs > 16 ? 16 : 22);
  const lh = fs * 1.1, y0 = y - ((lines.length - 1) * lh) / 2;
  lines.forEach((ln, i) => {
    const yy = y0 + i * lh;
    octx.lineWidth = 3.5; octx.strokeStyle = _labelHalo;
    octx.strokeText(ln, x, yy);
    octx.fillStyle = color || _labelText; octx.fillText(ln, x, yy);
  });
}

function drawOverlay() {
  const dpr = window.devicePixelRatio || 1;
  octx.clearRect(0, 0, overlay.width / dpr, overlay.height / dpr);
  octx.textAlign = "center"; octx.textBaseline = "middle";

  if (selected) {
    const sn = nodeByKey.get(selected);
    const zoom = map.getZoom();
    const drawn = [];
    const fp = P(selected), fc = map.project(proj(fp[0], fp[1]));
    octx.font = `600 12px ${getComputedStyle(document.body).fontFamily}`;
    // The dimmed normal icon stays under the fixed-size sel-points marker
    // (selection.js draws it at a constant px, doesn't shrink the original) —
    // for a big node (e.g. a heavily-starred repo) the original can be
    // larger than the marker and still poke out, so clear whichever is bigger.
    nodeNeighbors(selected).forEach(nk => {
      const nd = nodeByKey.get(nk), p = P(nk);
      if (!nd) return;
      const sc = map.project(proj(p[0], p[1]));
      const ndLabel = authorDisplayName(nd);
      const text = ndLabel.length > 24 ? ndLabel.slice(0, 23) + "…" : ndLabel;
      const m = _labelMetrics(text, 12);
      const yOff = Math.max(SEL_MARKER_PX.n, nodeScreenDiameter(nd, zoom)) / 2 + 8 + m.extra;
      const tw = octx.measureText(text).width;
      const boxH = m.lines * m.lh + 4;
      const box = { x: sc.x - tw / 2, y: sc.y - yOff - boxH / 2, w: tw, h: boxH };
      if (!drawn.some(b => !(box.x > b.x + b.w || box.x + box.w < b.x ||
          box.y > b.y + b.h || box.y + box.h < b.y))) {
        drawn.push(box);
        label(text, sc.x, sc.y - yOff, 12);
      }
    });
    const focusFs = sn.kind === "pub" ? 14 : 13;
    const snLabel = authorDisplayName(sn);
    const focusOff = Math.max(SEL_MARKER_PX.focus, nodeScreenDiameter(sn, zoom)) / 2 + 10 + _labelMetrics(snLabel, focusFs).extra;
    label(snLabel, fc.x, fc.y - focusOff, focusFs, nodeColor(sn));
    return;
  }

  if (selectedDept === null && map.getZoom() >= AUTHOR_LABEL_ZOOM) {
    drawEntityLabels(); return;
  }

  if (selectedDept !== null) {
    const d = deptById.get(selectedDept), c = deptCentroid.get(selectedDept);
    if (!d || !c) return;
    (DATA.dept_edges || [])
      .filter(e => (e.s === selectedDept || e.t === selectedDept) &&
        deptCentroid.has(e.s === selectedDept ? e.t : e.s))
      .sort((a, b) => b.w - a.w).slice(0, 8)
      .forEach(e => {
        const oid = e.s === selectedDept ? e.t : e.s;
        const pc = deptCentroid.get(oid); if (!pc) return;
        const sc = map.project(proj(pc[0], pc[1]));
        label(deptDisplayName(deptById.get(oid)) || "?", sc.x, sc.y, 12);
      });
    const sc = map.project(proj(c[0], c[1]));
    label(deptDisplayName(d), sc.x, sc.y, 16, d.color);
    return;
  }

  if (tab === 4 || tab === 2) return;
  const drawn = [];
  for (const d of DATA.departments) {
    const c = deptCentroid.get(d.id); if (!c) continue;
    const sc = map.project(proj(c[0], c[1]));
    if (sc.x < -50 || sc.x > innerWidth + 50 || sc.y < -50 || sc.y > innerHeight + 50) continue;
    const fs = Math.max(11, Math.min(map.getZoom() * 2, 17));
    const name = deptDisplayName(d);
    octx.font = `600 ${fs}px sans-serif`;
    const tw = octx.measureText(name).width;
    const box = { x: sc.x - tw / 2, y: sc.y - fs, w: tw, h: fs * 2.2 };
    if (drawn.some(b => !(box.x > b.x + b.w || box.x + box.w < b.x ||
        box.y > b.y + b.h || box.y + box.h < b.y))) continue;
    drawn.push(box);
    label(name, sc.x, sc.y, fs, d.color);
  }
}

function drawEntityLabels() {
  const b = map.getBounds();
  const xmin = (b.getWest()  / SPAN + 0.5) * S, xmax = (b.getEast()  / SPAN + 0.5) * S;
  const ymin = (0.5 - b.getNorth() / SPAN) * S, ymax = (0.5 - b.getSouth() / SPAN) * S;
  const cand = [];
  for (const nd of tabNodes()) {
    const p = P(nd.key);
    if (p[0] < xmin || p[0] > xmax || p[1] < ymin || p[1] > ymax) continue;
    cand.push(nd);
  }
  cand.sort((a, c) => szOf(c) - szOf(a));
  octx.font = `600 12px ${getComputedStyle(document.body).fontFamily}`;
  const zoom = map.getZoom();
  const drawn = []; let count = 0;
  for (const nd of cand) {
    if (count >= 160) break;
    const p = P(nd.key), sc = map.project(proj(p[0], p[1]));
    if (sc.x < -60 || sc.x > innerWidth + 60 || sc.y < -30 || sc.y > innerHeight + 30) continue;
    const ndLabel = authorDisplayName(nd);
    const tw = octx.measureText(ndLabel).width;
    const m = _labelMetrics(ndLabel, 12);
    const yOff = nodeScreenDiameter(nd, zoom) / 2 + 6 + m.extra;
    const boxH = m.lines * m.lh + 4;
    const box = { x: sc.x - tw / 2, y: sc.y - yOff - boxH / 2, w: tw, h: boxH };
    if (drawn.some(bb => !(box.x > bb.x + bb.w || box.x + box.w < bb.x ||
        box.y > bb.y + bb.h || box.y + box.h < bb.y))) continue;
    drawn.push(box);
    label(ndLabel, sc.x, sc.y - yOff, 12);
    count++;
  }
}