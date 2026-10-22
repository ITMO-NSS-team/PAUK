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
const PG_MAX_LINES = { pub: 4, section: 3, claim: 5, file: 2, source: 4 };
const PG_VIEW_H = 560;
// Sources hang under the claim they belong to, indented, so that opening
// them grows one band instead of adding a column the rest of the graph
// would have to make room for.
const PG_SRC_INDENT = 26, PG_SRC_W = PG_CLAIM_W - PG_SRC_INDENT, PG_SRC_GAP = 8;
const PG_SRC_LABEL_H = 14;
// A heading can have dozens of members; past this the rest is a count.
const PG_DOC_MAX = 10;
// Order a node's neighbours by how a reader moves through a paper: where
// it sits first, then what is next to it, then what it points at.
const PG_REL_ORDER = ["section_member<", "reading_order<", "reading_order>", "caption_of<",
  "caption_of>", "text_references>", "text_references<", "section_member>"];

let _pgKey = null;          // publication the current drawing belongs to
let _pgSelected = null;     // claim id shown in the detail block
let _pgOnlyWithCode = false;
let _pgShowSources = true;  // draw the picked claim's origin nodes
let _pgTransform = null;    // zoom kept across re-renders of the same paper
// Origin nodes opened into their document-graph neighbours, by path
// ("<claim>::<element>><element>..."): the same element can be reached by
// two routes and each opening belongs to its own.
let _pgOpen = new Set();

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

// Badges are set in small caps with tracking (style.css, .pg-badge); a
// neighbour's badge stacks several labels and must not run out of its box.
let _pgBadgeCtx = null;
function pgFitBadge(text, width) {
  if (!_pgBadgeCtx) {
    _pgBadgeCtx = document.createElement("canvas").getContext("2d");
    _pgBadgeCtx.font = '700 9.5px Inter, -apple-system, sans-serif';
  }
  const w = s => _pgBadgeCtx.measureText(s.toUpperCase()).width + s.length * 9.5 * 0.05;
  if (w(text) <= width) return text;
  while (text && w(text + "…") > width) text = text.slice(0, -1);
  return text.trimEnd() + "…";
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

// Neighbours of every element, built once per paper. "<type>>" is an edge
// leaving the element, "<type><" one arriving at it: a caption and its
// figure are one edge, read from opposite ends.
function pgDocIndex(graph) {
  if (graph._docIndex) return graph._docIndex;
  const doc = graph.doc || { nodes: {}, edges: [] };
  const around = new Map();
  const add = (from, to, rel, label) => {
    if (!around.has(from)) around.set(from, new Map());
    const seen = around.get(from);
    if (!seen.has(to)) seen.set(to, { id: to, rels: [], label: null });
    const entry = seen.get(to);
    if (!entry.rels.includes(rel)) entry.rels.push(rel);
    if (label) entry.label = label;
  };
  for (const [src, dst, type, label] of doc.edges) {
    add(src, dst, type + ">", label);
    add(dst, src, type + "<", label);
  }
  // Which claims each element is the origin of, so a neighbour can say it
  // also backs other claims.
  const claimsOf = new Map();
  for (const c of graph.claims) {
    for (const s of c.sources || []) {
      if (s.kind === "quote") continue;
      if (!claimsOf.has(s.id)) claimsOf.set(s.id, new Set());
      claimsOf.get(s.id).add(c.id);
    }
  }
  const rank = rel => { const i = PG_REL_ORDER.indexOf(rel); return i < 0 ? PG_REL_ORDER.length : i; };
  const neighbours = id => [...(around.get(id)?.values() || [])]
    .map(e => ({ ...e, rels: [...e.rels].sort((a, b) => rank(a) - rank(b)) }))
    .sort((a, b) => rank(a.rels[0]) - rank(b.rels[0])
      || (doc.nodes[a.id]?.page ?? 0) - (doc.nodes[b.id]?.page ?? 0));
  graph._docIndex = { nodes: doc.nodes, neighbours, claimsOf };
  return graph._docIndex;
}

// Only the claim you picked shows where it came from: every claim at once
// would bury the map of the paper under its own evidence. Each row is a box
// under the claim; opened origins bring their neighbours in the paper's
// document graph one level deeper, never back up the path they came down.
function pgSourceRows(graph, claim) {
  if (!_pgShowSources || claim.id !== _pgSelected) return [];
  const doc = pgDocIndex(graph);
  const rows = [];
  const push = (data, depth, parent, path, ancestors) => {
    const reachable = data.kind !== "quote" && doc.nodes[data.id]
      ? doc.neighbours(data.id).filter(n => !ancestors.has(n.id)) : [];
    const row = { data, depth, parent, path, expandable: reachable.length > 0,
      open: _pgOpen.has(path), total: reachable.length };
    rows.push(row);
    if (!row.open) return;
    const next = new Set(ancestors).add(data.id);
    for (const n of reachable.slice(0, PG_DOC_MAX)) {
      const others = [...(doc.claimsOf.get(n.id) || [])].filter(id => id !== claim.id).length;
      push({ kind: "doc", id: n.id, node: doc.nodes[n.id], rels: n.rels, label: n.label, others },
        depth + 1, row, `${path}>${n.id}`, next);
    }
  };
  for (const src of claim.sources || []) {
    push(src, 1, null, `${claim.id}::${src.id}`, new Set([src.id]));
  }
  return rows;
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

      const sources = pgSourceRows(graph, claim);
      const sourceBoxes = sources.map(row => {
        const w = PG_SRC_W - (row.depth - 1) * PG_SRC_INDENT;
        const text = row.data.kind === "doc" ? row.data.node?.text : row.data.text;
        return pgBox("source", w, text ? pgWrap(text, w - PG_PAD * 2, PG_MAX_LINES.source) : [],
          PG_SRC_LABEL_H);
      });
      const sourcesH = sourceBoxes.reduce((a, b) => a + b.h, 0)
        + Math.max(0, sourceBoxes.length - 1) * 6;
      const stackH = claimBox.h + (sourcesH ? PG_SRC_GAP + sourcesH : 0);
      const bandH = Math.max(stackH, filesH);

      const claimNode = { ...claimBox, id: claim.id, data: claim, x: x.claim,
        y: y + (bandH - stackH) / 2 };
      nodes.push(claimNode);
      claimNodes.push(claimNode);

      let sy = claimNode.y + claimBox.h + PG_SRC_GAP;
      sourceBoxes.forEach((box, i) => {
        const row = sources[i];
        const node = { ...box, id: `${claim.id}::s${i}`, data: row.data, row,
          x: x.claim + row.depth * PG_SRC_INDENT, y: sy };
        row.node = node;
        nodes.push(node);
        links.push({ from: row.parent ? row.parent.node : claimNode, to: node, kind: "source" });
        sy += box.h + 6;
      });

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
  if (link.kind === "source") {
    const x1 = link.from.x + PG_SRC_INDENT / 2, y1 = link.from.y + link.from.h;
    const x2 = link.to.x, y2 = link.to.y + link.to.h / 2;
    return `M${x1},${y1} C${x1},${y2} ${x1},${y2} ${x2},${y2}`;
  }
  const x1 = link.from.x + link.from.width, y1 = link.from.y + link.from.h / 2;
  const x2 = link.to.x, y2 = link.to.y + link.to.h / 2;
  const mx = x1 + (x2 - x1) / 2;
  return `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`;
}

// "quote · p. 4 · plain text" - what the box says about itself before its text.
// A document-graph neighbour leads with how it relates to the box above it.
function pgSourceLabel(src) {
  if (src.kind === "doc") {
    // Other claims first: when the badge is cut short, the element type is
    // the part a reader can best do without.
    return [src.rels.map(r => t("pg.rel." + r) || r).join(", "),
      src.others ? t("pg.alsoClaims", src.others) : "",
      src.node?.page != null ? t("pg.page", src.node.page) : "",
      src.label || src.node?.name || ""].filter(Boolean).join(" · ");
  }
  return [t("pg.src." + src.kind) || src.kind,
    src.page != null ? t("pg.page", src.page) : "",
    src.label || ""].filter(Boolean).join(" · ");
}

function pgClaimColor(claim) {
  return PG_CATEGORY_COLOR[claim.category] || PG_NEUTRAL;
}

function pgCategoryLabel(category) {
  return category ? t("pg.cat." + category) : t("pg.cat.unknown");
}

function pgNodeSvg(node) {
  const linesAt = (dy) => node.lines.map((line, i) =>
    `<tspan x="${node.x + PG_PAD}" y="${node.y + PG_PAD + dy + PG_LINE_H * (i + 0.78)}">${esc(line)}</tspan>`
  ).join("");
  const lines = linesAt(0);

  if (node.kind === "source") {
    const src = node.data, row = node.row;
    const toggle = row.expandable ? (row.open ? "▾ " : "▸ ") : "";
    const shown = row.open && row.total > PG_DOC_MAX ? " · " + t("pg.shownOf", PG_DOC_MAX, row.total) : "";
    const badge = pgSourceLabel(src) + shown;
    const full = [badge, src.kind === "doc" ? src.node?.text : src.text].filter(Boolean).join("\n\n");
    return `<g class="pg-node pg-source pg-source-${esc(src.kind)}${row.expandable ? " pg-expandable" : ""}${row.open ? " pg-open" : ""}"${row.expandable ? ` data-pg-expand="${esc(row.path)}"` : ""}>
      <title>${esc(full)}</title>
      <rect x="${node.x}" y="${node.y}" width="${node.width}" height="${node.h}" rx="6" />
      <text class="pg-badge pg-source-badge" x="${node.x + PG_PAD}" y="${node.y + PG_PAD + 8}">${esc(pgFitBadge(toggle + badge, node.width - PG_PAD * 2))}</text>
      <text class="pg-text pg-source-text">${linesAt(PG_SRC_LABEL_H)}</text>
    </g>`;
  }

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

  // The quote is the blockquote above; here goes what only a parsed PDF knows.
  const origin = (claim.sources || []).filter(s => s.kind !== "quote");

  return `<div class="pg-detail-head">
      <span class="pg-chip" style="border-color:${color};color:${color}">${esc(pgCategoryLabel(claim.category))}</span>
      ${claim.verifiability ? `<span class="pg-chip">${esc(t("pg.ver." + claim.verifiability))}</span>` : ""}
      ${claim.contradiction ? `<span class="pg-chip pg-chip-warn">${esc(t("pg.contradiction"))}</span>` : ""}
      ${claim.section ? `<span class="pg-chip pg-chip-plain">${esc(pgSectionName(graph, claim.section))}</span>` : ""}
    </div>
    <div class="pg-claim-text">${esc(claim.claim)}</div>
    ${rows.join("")}
    <div class="pg-detail-sub">${esc(t("pg.filesTitle"))}</div>
    ${filesHtml}
    ${origin.length ? `<div class="pg-detail-sub">${esc(t("pg.sourcesTitle"))}</div>
      <ul class="pg-source-list">${origin.map(src =>
        `<li><b>${esc(pgSourceLabel(src))}</b>${src.text ? " — " + esc(src.text) : ""}</li>`
      ).join("")}</ul>` : ""}`;
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
  const keepView = _pgKey === key && _pgTransform;
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
      <button class="pg-btn${_pgShowSources ? " active" : ""}" data-pg-toggle="sources" title="${esc(t("pg.showSourcesHint"))}">${esc(t("pg.showSources"))}</button>
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
    .on("zoom", e => {
      root.setAttribute("transform", e.transform.toString());
      _pgTransform = e.transform;
    });
  d3.select(svg).call(zoom);
  // Picking a claim re-draws the whole card; refitting on every pick would
  // throw away the pan and zoom the reader just set. Opening its sources
  // can still push the claim past the bottom edge, so the kept view is
  // nudged - and only nudged - until the claim it belongs to is visible.
  if (keepView) {
    const sel = layout.nodes.find(n => n.kind === "claim" && n.id === _pgSelected);
    let tr = _pgTransform;
    if (sel) {
      const own = layout.nodes.filter(n => n.kind === "source" && n.data && n.id.startsWith(sel.id + "::"));
      const top = sel.y, bottom = Math.max(sel.y + sel.h, ...own.map(n => n.y + n.h));
      const yTop = tr.applyY(top), yBottom = tr.applyY(bottom);
      if (yBottom > viewH - 8) tr = tr.translate(0, (viewH - 8 - yBottom) / tr.k);
      else if (yTop < 8) tr = tr.translate(0, (8 - yTop) / tr.k);
    }
    d3.select(svg).call(zoom.transform, tr);
  } else fit();

  host.querySelectorAll("[data-claim]").forEach(el => {
    el.onclick = () => {
      const id = el.getAttribute("data-claim");
      _pgSelected = _pgSelected === id ? null : id;
      _pgOpen = new Set();
      if (_pgShowSources) { pgRenderCard(host, key); return; }  // the layout changed
      host.querySelectorAll(".pg-claim").forEach(n => n.classList.remove("pg-sel"));
      if (_pgSelected) el.classList.add("pg-sel");
      document.getElementById("pg-detail").innerHTML =
        pgDetailHtml(graph, claims.find(c => c.id === _pgSelected));
    };
  });
  host.querySelectorAll("[data-pg-expand]").forEach(el => {
    el.onclick = () => {
      const path = el.getAttribute("data-pg-expand");
      if (_pgOpen.has(path)) {
        // Closing a box closes everything opened beneath it too.
        for (const p of [..._pgOpen]) if (p === path || p.startsWith(path + ">")) _pgOpen.delete(p);
      } else _pgOpen.add(path);
      pgRenderCard(host, key);
    };
  });
  host.querySelector('[data-pg-toggle="code"]').onclick = () => {
    _pgOnlyWithCode = !_pgOnlyWithCode;
    pgRenderCard(host, key);
  };
  host.querySelector('[data-pg-toggle="sources"]').onclick = () => {
    _pgShowSources = !_pgShowSources;
    pgRenderCard(host, key);
  };
  host.querySelector("[data-pg-fit]").onclick = fit;

  document.getElementById("pg-detail").innerHTML =
    pgDetailHtml(graph, claims.find(c => c.id === _pgSelected));
}

// A profile switch starts over: the selection belongs to the paper it was
// made in, and carrying it across would highlight nothing.
function pgResetFor(key) {
  if (key !== _pgKey) {
    _pgSelected = null; _pgOnlyWithCode = false; _pgTransform = null; _pgOpen = new Set();
  }
}
