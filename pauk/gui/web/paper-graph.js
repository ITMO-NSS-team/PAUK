"use strict";

// The graph of one paper: publication -> sections -> claims -> repository
// files, drawn inside the publication profile (tab 4). Data comes from
// paper-graphs.js (window.PAPER_GRAPHS), which is generated from OSA
// exports and is absent on an install that has none - every entry point
// here treats "no data" as the normal case.

const PG_CATEGORY_COLOR = {
  dataset:            "#3f8f6f",
  model_architecture: "#3555e0",
  training_procedure: "#8a5cd6",
  evaluation_metric:  "#0f8ea8",
  numerical_result:   "#d9962e",
  baseline_comparison:"#c0562e",
  data_preprocessing: "#5d7a2e",
  infrastructure:     "#7a7f88",
};
const PG_NEUTRAL = "#7a7f88";

// Geometry. Columns are fixed-width so that wrapping is decided once, at
// layout time, instead of on every zoom step.
const PG_PUB_W = 190, PG_SEC_W = 170, PG_CLAIM_W = 300, PG_FILE_W = 220;
const PG_COL_GAP = 54, PG_ROW_GAP = 10, PG_PAD = 9, PG_LINE_H = 15;
const PG_FONT = '12px Inter, -apple-system, sans-serif';
const PG_MAX_LINES = { pub: 4, section: 3, claim: 5, file: 2 };
const PG_VIEW_H = 560;

let _pgKey = null;          // publication the current drawing belongs to
let _pgSelected = null;     // claim id shown in the detail block
let _pgOnlyWithCode = false;

function paperGraphOf(key) {
  const all = window.PAPER_GRAPHS;
  return (all && all[key]) || null;
}

// paper-graphs.js loads after this file (like graph-search.js): if a
// profile is already on screen when it arrives, draw it now.
window._onPaperGraphsReady = function () {
  if (typeof tab !== "undefined" && tab === 4 && typeof _spRefreshCurrentView === "function") {
    _spRefreshCurrentView();
  }
};

// ---------- text ----------

let _pgMeasureCtx = null;
function pgTextWidth(s) {
  if (!_pgMeasureCtx) {
    _pgMeasureCtx = document.createElement("canvas").getContext("2d");
    _pgMeasureCtx.font = PG_FONT;
  }
  return _pgMeasureCtx.measureText(s).width;
}

function pgWrap(text, width, maxLines) {
  const words = String(text || "").split(/\s+/).filter(Boolean);
  const lines = [];
  let line = "";
  for (const w of words) {
    const next = line ? line + " " + w : w;
    if (line && pgTextWidth(next) > width) {
      lines.push(line);
      line = w;
      if (lines.length === maxLines) break;
    } else {
      line = next;
    }
  }
  if (lines.length < maxLines && line) lines.push(line);
  if (!lines.length) return [""];
  // Trim the last line down until the ellipsis fits, so a long claim ends
  // readably instead of running past its box.
  const consumed = lines.join(" ").split(/\s+/).length;
  if (consumed < words.length) {
    let last = lines[lines.length - 1];
    while (last && pgTextWidth(last + " …") > width) last = last.slice(0, -1);
    lines[lines.length - 1] = last + " …";
  }
  return lines;
}

// ---------- layout ----------

function pgClaimsBySection(graph) {
  const by = new Map();
  for (const c of graph.claims) {
    if (_pgOnlyWithCode && !(c.files || []).length) continue;
    const id = c.section || "";
    if (!by.has(id)) by.set(id, []);
    by.get(id).push(c);
  }
  return by;
}

function pgBox(kind, width, lines, extraH) {
  return { kind, width, lines, h: PG_PAD * 2 + lines.length * PG_LINE_H + (extraH || 0) };
}

// Publication -> sections -> claims -> files, stacked top to bottom in one
// pass: each claim owns a band as tall as itself or its files, whichever is
// taller, and a section centres on the band its claims occupy.
function pgLayout(graph, title) {
  const bySection = pgClaimsBySection(graph);
  const sections = graph.sections.filter(s => bySection.has(s.id));
  if (bySection.has("")) {
    sections.push({ id: "", name: t("pg.noSection"), level: 1, orphan: true });
  }

  const x = {
    pub: 0,
    section: PG_PUB_W + PG_COL_GAP,
    claim: PG_PUB_W + PG_COL_GAP + PG_SEC_W + PG_COL_GAP,
    file: PG_PUB_W + PG_COL_GAP + PG_SEC_W + PG_COL_GAP + PG_CLAIM_W + PG_COL_GAP,
  };
  const nodes = [], links = [];
  let y = 0, widest = x.claim + PG_CLAIM_W;

  for (const section of sections) {
    const claims = bySection.get(section.id) || [];
    const sectionTop = y;
    const claimNodes = [];

    for (const claim of claims) {
      const claimLines = pgWrap(claim.claim, PG_CLAIM_W - PG_PAD * 2, PG_MAX_LINES.claim);
      const claimBox = pgBox("claim", PG_CLAIM_W, claimLines, 16); // + badge row
      const files = claim.files || [];
      const fileBoxes = files.map(f =>
        pgBox("file", PG_FILE_W, pgWrap(f.path, PG_FILE_W - PG_PAD * 2, PG_MAX_LINES.file)));
      const filesH = fileBoxes.reduce((a, b) => a + b.h, 0) + Math.max(0, fileBoxes.length - 1) * 6;
      const bandH = Math.max(claimBox.h, filesH);

      const claimNode = { ...claimBox, id: claim.id, data: claim, x: x.claim,
        y: y + (bandH - claimBox.h) / 2 };
      nodes.push(claimNode);
      claimNodes.push(claimNode);

      let fy = y + (bandH - filesH) / 2;
      fileBoxes.forEach((box, i) => {
        const node = { ...box, id: `${claim.id}::f${i}`, data: files[i], x: x.file, y: fy,
          claimId: claim.id };
        nodes.push(node);
        links.push({ from: claimNode, to: node, kind: "file" });
        widest = Math.max(widest, x.file + PG_FILE_W);
        fy += box.h + 6;
      });
      y += bandH + PG_ROW_GAP;
    }

    const label = (section.numbering ? section.numbering + " " : "") + section.name;
    const secBox = pgBox("section", PG_SEC_W, pgWrap(label, PG_SEC_W - PG_PAD * 2, PG_MAX_LINES.section));
    const bandBottom = y - PG_ROW_GAP;
    const secNode = { ...secBox, id: `sec:${section.id}`, data: section, x: x.section,
      y: (sectionTop + bandBottom) / 2 - secBox.h / 2 };
    nodes.push(secNode);
    claimNodes.forEach(c => links.push({ from: secNode, to: c, kind: "claim" }));
    y += PG_ROW_GAP * 2;
  }

  const height = Math.max(1, y - PG_ROW_GAP * 2);
  const pubBox = pgBox("pub", PG_PUB_W, pgWrap(title, PG_PUB_W - PG_PAD * 2, PG_MAX_LINES.pub));
  const pubNode = { ...pubBox, id: "pub", x: x.pub, y: height / 2 - pubBox.h / 2 };
  nodes.push(pubNode);
  nodes.filter(n => n.kind === "section").forEach(s => links.push({ from: pubNode, to: s, kind: "section" }));

  return { nodes, links, width: widest, height };
}

// ---------- drawing ----------

function pgLinkPath(link) {
  const x1 = link.from.x + link.from.width, y1 = link.from.y + link.from.h / 2;
  const x2 = link.to.x, y2 = link.to.y + link.to.h / 2;
  const mx = x1 + (x2 - x1) / 2;
  return `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`;
}

function pgClaimColor(claim) {
  return PG_CATEGORY_COLOR[claim.category] || PG_NEUTRAL;
}

function pgCategoryLabel(category) {
  return category ? t("pg.cat." + category) : t("pg.cat.unknown");
}

function pgNodeSvg(node) {
  const lines = node.lines.map((line, i) =>
    `<tspan x="${node.x + PG_PAD}" y="${node.y + PG_PAD + PG_LINE_H * (i + 0.78)}">${esc(line)}</tspan>`
  ).join("");

  if (node.kind === "claim") {
    const claim = node.data;
    const color = pgClaimColor(claim);
    const withCode = (claim.files || []).length > 0;
    const badge = pgCategoryLabel(claim.category);
    return `<g class="pg-node pg-claim${_pgSelected === claim.id ? " pg-sel" : ""}${withCode ? "" : " pg-nocode"}" data-claim="${esc(claim.id)}">
      <rect x="${node.x}" y="${node.y}" width="${node.width}" height="${node.h}" rx="8"
            style="stroke:${color}" />
      <rect x="${node.x}" y="${node.y}" width="3" height="${node.h}" style="fill:${color};stroke:none" />
      <text class="pg-text">${lines}</text>
      <text class="pg-badge" x="${node.x + PG_PAD}" y="${node.y + node.h - PG_PAD}" style="fill:${color}">${esc(badge)}${claim.contradiction ? " · " + esc(t("pg.contradiction")) : ""}</text>
    </g>`;
  }

  if (node.kind === "file") {
    const file = node.data;
    const body = `<g class="pg-node pg-file">
      <rect x="${node.x}" y="${node.y}" width="${node.width}" height="${node.h}" rx="6" />
      <text class="pg-text pg-mono">${lines}</text>
    </g>`;
    return file.url
      ? `<a href="${esc(file.url)}" target="_blank" rel="noopener noreferrer">${body}</a>`
      : body;
  }

  const cls = node.kind === "pub" ? "pg-pub" : "pg-section";
  return `<g class="pg-node ${cls}">
    <rect x="${node.x}" y="${node.y}" width="${node.width}" height="${node.h}" rx="8" />
    <text class="pg-text">${lines}</text>
  </g>`;
}

function pgDetailHtml(graph, claim) {
  if (!claim) return `<div class="pg-detail-hint">${esc(t("pg.pickClaim"))}</div>`;
  const color = pgClaimColor(claim);
  const files = claim.files || [];
  const rows = [];
  if (claim.value) rows.push(`<div class="pg-row"><b>${esc(t("pg.value"))}</b> ${esc(claim.value)}</div>`);
  if (claim.evidence) rows.push(`<blockquote class="pg-quote">${esc(claim.evidence)}</blockquote>`);
  if (claim.correspondence != null)
    rows.push(`<div class="pg-row"><b>${esc(t("pg.correspondence"))}</b> ${Math.round(claim.correspondence)}%</div>`);
  if ((claim.missing || []).length)
    rows.push(`<div class="pg-row"><b>${esc(t("pg.missing"))}</b> ${esc(claim.missing.join("; "))}</div>`);
  if (claim.reasoning) rows.push(`<div class="pg-row pg-muted">${esc(claim.reasoning)}</div>`);

  const filesHtml = files.length
    ? `<ul class="pg-files">${files.map(f => `<li>${f.url
        ? `<a href="${esc(f.url)}" target="_blank" rel="noopener noreferrer">${esc(f.path)}</a>`
        : esc(f.path)}</li>`).join("")}</ul>`
    : `<div class="pg-row pg-muted">${esc(t("pg.noFiles"))}</div>`;

  return `<div class="pg-detail-head">
      <span class="pg-chip" style="border-color:${color};color:${color}">${esc(pgCategoryLabel(claim.category))}</span>
      ${claim.verifiability ? `<span class="pg-chip">${esc(t("pg.ver." + claim.verifiability))}</span>` : ""}
      ${claim.contradiction ? `<span class="pg-chip pg-chip-warn">${esc(t("pg.contradiction"))}</span>` : ""}
      ${claim.section ? `<span class="pg-chip pg-chip-plain">${esc(pgSectionName(graph, claim.section))}</span>` : ""}
    </div>
    <div class="pg-claim-text">${esc(claim.claim)}</div>
    ${rows.join("")}
    <div class="pg-detail-sub">${esc(t("pg.filesTitle"))}</div>
    ${filesHtml}`;
}

function pgSectionName(graph, id) {
  const s = graph.sections.find(x => x.id === id);
  if (!s) return t("pg.noSection");
  return (s.numbering ? s.numbering + " " : "") + s.name;
}

// ---------- card ----------

function pgRenderCard(host, key) {
  const graph = paperGraphOf(key);
  if (!host || !graph) return;
  _pgKey = key;
  const node = nodeByKey.get(key);
  const title = graph.title || node?.label || key;
  const claims = graph.claims || [];
  const withCode = claims.filter(c => (c.files || []).length).length;
  const categories = [...new Set(claims.map(c => c.category || ""))];

  host.innerHTML = `
    <div class="sp-card-title">${esc(t("pg.title"))}</div>
    <div class="pg-stats">
      <span><b>${claims.length}</b> ${esc(t("pg.claims"))}</span>
      <span><b>${graph.sections.length}</b> ${esc(t("pg.sections"))}</span>
      <span><b>${withCode}</b> ${esc(t("pg.withCode"))}</span>
      ${graph.repo_url ? `<a href="${esc(graph.repo_url)}" target="_blank" rel="noopener noreferrer">${esc(graph.repo_url.replace("https://github.com/", ""))}</a>` : ""}
    </div>
    <div class="pg-controls">
      <button class="pg-btn${_pgOnlyWithCode ? " active" : ""}" data-pg-toggle="code">${esc(t("pg.onlyWithCode"))}</button>
      <button class="pg-btn" data-pg-fit="1">${esc(t("pg.fit"))}</button>
      <span class="pg-legend">${categories.map(c =>
        `<span class="pg-legend-item"><i style="background:${PG_CATEGORY_COLOR[c] || PG_NEUTRAL}"></i>${esc(pgCategoryLabel(c))}</span>`
      ).join("")}</span>
    </div>
    <div class="pg-canvas"><svg class="pg-svg"><g class="pg-root"></g></svg></div>
    <div class="pg-detail" id="pg-detail"></div>`;

  const svg = host.querySelector(".pg-svg");
  const root = host.querySelector(".pg-root");
  const layout = pgLayout(graph, title);
  root.innerHTML =
    layout.links.map(l => `<path class="pg-link pg-link-${l.kind}" d="${pgLinkPath(l)}" />`).join("") +
    layout.nodes.map(pgNodeSvg).join("");

  const viewH = Math.min(PG_VIEW_H, layout.height + 24);
  svg.setAttribute("height", viewH);
  svg.setAttribute("width", "100%");

  const fit = () => {
    const w = svg.clientWidth || layout.width;
    const k = Math.min(1, (w - 16) / layout.width, viewH / (layout.height + 16));
    const tr = d3.zoomIdentity.translate(8, (viewH - layout.height * k) / 2).scale(k);
    d3.select(svg).call(zoom.transform, tr);
  };
  const zoom = d3.zoom().scaleExtent([0.25, 2.5])
    .on("zoom", e => root.setAttribute("transform", e.transform.toString()));
  d3.select(svg).call(zoom);
  fit();

  host.querySelectorAll("[data-claim]").forEach(el => {
    el.onclick = () => {
      const id = el.getAttribute("data-claim");
      _pgSelected = _pgSelected === id ? null : id;
      host.querySelectorAll(".pg-claim").forEach(n => n.classList.remove("pg-sel"));
      if (_pgSelected) el.classList.add("pg-sel");
      document.getElementById("pg-detail").innerHTML =
        pgDetailHtml(graph, claims.find(c => c.id === _pgSelected));
    };
  });
  host.querySelector("[data-pg-toggle]").onclick = () => {
    _pgOnlyWithCode = !_pgOnlyWithCode;
    pgRenderCard(host, key);
  };
  host.querySelector("[data-pg-fit]").onclick = fit;

  document.getElementById("pg-detail").innerHTML =
    pgDetailHtml(graph, claims.find(c => c.id === _pgSelected));
}

// A profile switch starts over: the selection belongs to the paper it was
// made in, and carrying it across would highlight nothing.
function pgResetFor(key) {
  if (key !== _pgKey) { _pgSelected = null; _pgOnlyWithCode = false; }
}
