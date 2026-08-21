"use strict";

// Tab 5 — "Здоровье БД". Renders window.STATS, which the page loads as a
// static file; the "Пересчитать" button asks the server for a fresh snapshot
// straight from Neo4j and re-renders in place. Clicking a check opens a popup
// with the rows behind it, which can also be saved as CSV.

const ST_STATUS = {
  fail: { label: t("st.status.fail"), cls: "st-fail" },
  warn: { label: t("st.status.warn"), cls: "st-warn" },
  ok:   { label: t("st.status.ok"),   cls: "st-ok"   },
  error: { label: t("st.status.error"), cls: "st-err-check" },
};
const ST_ORDER = { error: 0, fail: 1, warn: 2, ok: 3 };

const ST_VIEW_LIMIT = 300;    // строк в окне
const ST_CSV_LIMIT  = 5000;   // строк в выгрузке

let _stBusy = false;
let _stError = "";

const stNum = n => (n == null ? "—" : n.toLocaleString("ru-RU").replace(/ /g, " "));

// Списки приходят массивами (например, идентификаторы группы дублей).
const stCell = v => Array.isArray(v) ? v.join(", ") : (v == null ? "" : String(v));

// Check titles/hints/groups and the node/rel inventory come from Python
// bilingually (field + field_en) — examples-table columns don't yet, they
// stay Russian regardless of LANG.
const loc = (ru, en) => (LANG === "en" && en) ? en : ru;

function stTiles(s) {
  // Lookup keys below match generate_stats.py's NODE_COUNTS labels exactly —
  // they're data keys, not chrome, so they stay Russian regardless of LANG.
  const by = l => (s.nodes.find(n => n.label === l) || {}).n;
  const tiles = [
    [t("st.tile.pubs"),      by("Публикации")],
    [t("st.tile.itmoStaff"), by("— сотрудники ИТМО")],
    [t("st.tile.repos"),     by("Репозитории")],
    [t("st.tile.depts"),     by("Департаменты")],
    [t("st.tile.links"),     s.totals.rels],
  ];
  return `<div class="st-tiles">` + tiles.map(([k, v]) =>
    `<div class="st-tile"><div class="st-tile-n">${stNum(v)}</div>
       <div class="st-tile-k">${esc(k)}</div></div>`).join("") + `</div>`;
}

// One list component for every "название — число" block on the page.
function stList(title, items, labelOf, noteOf) {
  return `<div class="st-col"><div class="st-col-h">${esc(title)}</div>` + items.map(i => {
    const note = noteOf ? noteOf(i) : "";
    return `<div class="st-row">
      <span class="st-row-k">${esc(labelOf(i))}${
        note ? `<span class="st-row-note">${esc(note)}</span>` : ""}</span>
      <span class="st-row-n">${stNum(i.n)}</span>
    </div>`;
  }).join("") + `</div>`;
}

function stChecks(checks) {
  const groups = [];
  checks.forEach(c => {
    let g = groups.find(x => x.name === c.group);
    if (!g) groups.push(g = { name: c.group, name_en: c.group_en, items: [] });
    g.items.push(c);
  });

  return groups.map(g => {
    const items = g.items.slice().sort((a, b) =>
      ST_ORDER[a.status] - ST_ORDER[b.status] || (b.pct || 0) - (a.pct || 0) || b.n - a.n);
    return `<div class="st-block"><div class="st-col-h">${esc(loc(g.name, g.name_en))}</div>` + items.map(c => {
      const st = ST_STATUS[c.status] || ST_STATUS.ok;
      const clickable = c.has_examples && c.n > 0;
      const hint = loc(c.hint, c.hint_en);
      return `<div class="st-check ${st.cls}${clickable ? " st-clickable" : ""}"${
        clickable ? ` data-check="${esc(c.id)}" tabindex="0" role="button"` : ""}>
        <span class="st-dot" aria-hidden="true"></span>
        <div class="st-check-body">
          <div class="st-check-top">
            <span class="st-check-t">${esc(loc(c.title, c.title_en))}${
              clickable ? `<span class="st-check-more">${t("st.examples")}</span>` : ""}</span>
            <span class="st-check-v">${stNum(c.n)}${
              c.pct != null ? ` <span class="st-check-pct">${c.pct}%</span>` : ""
            }</span>
          </div>
          ${hint ? `<div class="st-check-hint">${esc(hint)}</div>` : ""}
        </div>
        <span class="st-check-st">${st.label}</span>
      </div>`;
    }).join("") + `</div>`;
  }).join("");
}

// ---------- popup with the rows behind a check ----------

function stModalEl() {
  let m = document.getElementById("st-modal");
  if (m) return m;
  m = document.createElement("div");
  m.id = "st-modal";
  m.innerHTML = `<div class="st-modal-box" role="dialog" aria-modal="true">
      <div class="st-modal-head">
        <div>
          <div class="st-modal-title"></div>
          <div class="st-modal-sub"></div>
        </div>
        <div class="st-modal-actions">
          <button class="st-btn" data-act="map">${t("st.modal.highlight")}</button>
          <button class="st-btn" data-act="csv">${t("st.modal.downloadCsv")}</button>
          <button class="st-btn st-modal-x" data-act="close" aria-label="${t("st.modal.close")}">×</button>
        </div>
      </div>
      <div class="st-modal-body"></div>
    </div>`;
  document.body.appendChild(m);
  m.addEventListener("click", e => { if (e.target === m) stCloseModal(); });
  m.querySelector('[data-act="close"]').onclick = stCloseModal;
  return m;
}

function stCloseModal() {
  const m = document.getElementById("st-modal");
  if (m) m.classList.remove("visible");
  document.removeEventListener("keydown", stEscClose);
}

function stEscClose(e) { if (e.key === "Escape") stCloseModal(); }

// Read an /api/ response. Parsing before checking the status turns any
// non-JSON answer — the HTML 404 a static-only server gives when it has no
// /api/ routes, a proxy error page — into an unreadable parser message
// ("Unexpected token '<'"), so decode the body first and only then decide.
async function stApiJson(response) {
  const text = await response.text();
  let body;
  try {
    body = JSON.parse(text);
  } catch {
    throw new Error(response.ok
      ? t("st.err.notJson")
      : t("st.err.oldServer", response.status));
  }
  if (!response.ok) throw new Error(body.error || t("st.err.serverError", response.status));
  return body;
}

async function stFetchExamples(id, limit) {
  const r = await fetch(`/api/check?id=${encodeURIComponent(id)}&limit=${limit}`,
                        { cache: "no-store" });
  return stApiJson(r);
}

async function stOpenExamples(id) {
  const m = stModalEl();
  m.classList.add("visible");
  document.addEventListener("keydown", stEscClose);
  m.querySelector(".st-modal-title").textContent = t("st.modal.examplesTitle");
  m.querySelector(".st-modal-sub").textContent = "";
  m.querySelector(".st-modal-body").innerHTML = `<div class="st-modal-msg">${t("st.modal.loading")}</div>`;
  m.querySelector('[data-act="csv"]').onclick = () => stDownloadCsv(id);
  m.querySelector('[data-act="map"]').onclick = () => stHighlightOnMap(id);

  let d;
  try {
    d = await stFetchExamples(id, ST_VIEW_LIMIT);
  } catch (e) {
    m.querySelector(".st-modal-body").innerHTML =
      `<div class="st-modal-msg">${esc(e.message === "Failed to fetch"
        ? t("st.err.serverUnavailable") : e.message)}</div>`;
    return;
  }

  const dHint = loc(d.hint, d.hint_en);
  m.querySelector(".st-modal-title").textContent = loc(d.title, d.title_en);
  m.querySelector(".st-modal-sub").textContent =
    (dHint ? dHint + " · " : "") +
    (d.truncated ? t("st.modal.shownOf", stNum(d.shown), stNum(d.total))
                 : t("st.modal.totalRows", stNum(d.total)));

  if (!d.rows.length) {
    m.querySelector(".st-modal-body").innerHTML =
      `<div class="st-modal-msg">${t("st.modal.notFound")}</div>`;
    return;
  }

  m.querySelector(".st-modal-body").innerHTML = `<table class="st-table">
    <thead><tr>${d.columns.map(c => `<th>${esc(c)}</th>`).join("")}</tr></thead>
    <tbody>${d.rows.map(r =>
      `<tr>${r.map(v => `<td>${esc(stCell(v))}</td>`).join("")}</tr>`).join("")}</tbody>
  </table>`;
}

function stToCsv(columns, rows) {
  const q = v => {
    const s = stCell(v);
    return /[",;\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  };
  // ; как разделитель и BOM — иначе Excel не откроет кириллицу корректно
  return "﻿" + [columns.map(q).join(";")]
    .concat(rows.map(r => r.map(q).join(";"))).join("\r\n");
}

async function stDownloadCsv(id) {
  const m = stModalEl();
  const btn = m.querySelector('[data-act="csv"]');
  const was = btn.textContent;
  btn.disabled = true; btn.textContent = t("st.csv.preparing");
  try {
    const d = await stFetchExamples(id, ST_CSV_LIMIT);
    const blob = new Blob([stToCsv(d.columns, d.rows)],
                          { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `pauk-${id}.csv`;
    document.body.appendChild(a);   // Firefox и Safari игнорируют клик вне DOM
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  } catch (e) {
    alert(e.message === "Failed to fetch" ? t("st.err.serverUnavailable") : e.message);
  } finally {
    btn.disabled = false; btn.textContent = was;
  }
}

// ---------- highlighting the offending entities on the map ----------

// Red is a deliberate exception to the grayscale rule: on the map colour
// already carries meaning, and the point here is to stand out against it.
const ST_HL_COLOR = "#e03131";
const ST_HL_DIM   = 0.05;
// Only nodes are marked: every check resolves to offending entities, not to
// the links between them, so painting incident edges red implied a problem
// with the relationship that isn't there.
const ST_HL_LAYERS = ["st-problem-nodes"];

// Ключи узлов карты совпадают с идентификаторами в Neo4j, поэтому их можно
// вытащить прямо из строк примеров, не заводя отдельный запрос под каждую
// проверку.
const ST_KEY_KIND = [
  [/^itmo_A\d+$/, "author"],
  [/^W\d+$/,      "pub"],
  [/^repo_[0-9a-f]+$/, "repo"],
];
const ST_KIND_TAB = { author: 1, repo: 2, pub: 3 };

function stKeysFromRows(rows) {
  const found = { author: [], pub: [], repo: [] };
  const seen = new Set();
  const take = v => {
    if (Array.isArray(v)) return v.forEach(take);
    if (typeof v !== "string" || seen.has(v)) return;
    for (const [re, kind] of ST_KEY_KIND) {
      if (re.test(v)) { seen.add(v); found[kind].push(v); return; }
    }
  };
  rows.forEach(r => r.forEach(take));
  return found;
}

function stClearHighlight() {
  ST_HL_LAYERS.forEach(id => { if (map.getLayer(id)) map.removeLayer(id); });
  if (map.getLayer("authors")) map.setPaintProperty("authors", "icon-opacity", NODE_OPACITY);
  if (map.getLayer("repos"))
    map.setPaintProperty("repos", "icon-opacity", tab === 2 ? 1 : NODE_OPACITY);
  if (map.getLayer("pubs")) map.setPaintProperty("pubs", "icon-opacity", NODE_OPACITY);
  if (map.getLayer("edges"))
    map.setPaintProperty("edges", "line-opacity",
      tab === 2 ? 1 : tab === 3 ? EDGE_OPACITY_PUBS : EDGE_OPACITY_COAUTH);
  if (map.getLayer("dept-fill")) map.setPaintProperty("dept-fill", "fill-opacity", FILL_OPACITY);
  if (map.getLayer("dept-line")) map.setPaintProperty("dept-line", "line-opacity", 0.8);
  if (map.getLayer("dept-edges")) map.setPaintProperty("dept-edges", "line-opacity", DEPT_EDGE_OPACITY);
  const b = document.getElementById("st-hl-bar");
  if (b) b.classList.remove("visible");
}

function stApplyHighlight(keys, title) {
  const lit = ["literal", keys];

  if (map.getLayer("authors")) map.setPaintProperty("authors", "icon-opacity", ST_HL_DIM);
  if (map.getLayer("repos"))   map.setPaintProperty("repos", "icon-opacity", ST_HL_DIM);
  if (map.getLayer("pubs"))    map.setPaintProperty("pubs", "icon-opacity", ST_HL_DIM);
  if (map.getLayer("edges"))   map.setPaintProperty("edges", "line-opacity", 0.03);
  if (map.getLayer("dept-fill")) map.setPaintProperty("dept-fill", "fill-opacity", 0.04);
  if (map.getLayer("dept-line")) map.setPaintProperty("dept-line", "line-opacity", 0.1);
  if (map.getLayer("dept-edges")) map.setPaintProperty("dept-edges", "line-opacity", 0.04);

  ST_HL_LAYERS.forEach(id => { if (map.getLayer(id)) map.removeLayer(id); });

  map.addLayer({
    id: "st-problem-nodes", type: "circle", source: "nodes",
    filter: ["in", ["get", "key"], lit],
    paint: {
      "circle-color": ST_HL_COLOR,
      "circle-radius": ["interpolate", ["linear"], ["zoom"],
        3, ["*", 1.6, ["get", "sz"]], 9, ["*", 7, ["get", "sz"]]],
      "circle-stroke-color": "#ffffff", "circle-stroke-width": 1.2,
      "circle-opacity": 0.95,
    },
  });

  stHighlightBar(keys.length, title);
}

function stHighlightBar(n, title) {
  let b = document.getElementById("st-hl-bar");
  if (!b) {
    b = document.createElement("div");
    b.id = "st-hl-bar";
    b.innerHTML = `<span class="st-hl-dot"></span><span class="st-hl-text"></span>
      <button class="st-btn" data-act="reset">${t("st.hl.reset")}</button>`;
    document.body.appendChild(b);
    b.querySelector('[data-act="reset"]').onclick = stClearHighlight;
  }
  b.querySelector(".st-hl-text").textContent = t("st.hl.text", title, stNum(n));
  b.classList.add("visible");
}

async function stHighlightOnMap(id, title) {
  const m = stModalEl();
  const btn = m.querySelector('[data-act="map"]');
  const was = btn.textContent;
  btn.disabled = true; btn.textContent = t("st.map.searching");
  try {
    const d = await stFetchExamples(id, ST_CSV_LIMIT);
    const found = stKeysFromRows(d.rows);
    const best = ["author", "pub", "repo"]
      .map(k => [k, found[k].length]).sort((a, b) => b[1] - a[1])[0];

    if (!best[1]) {
      // Не утверждаем причину: строки могут быть о департаментах и ссылках,
      // которых на карте нет, а могут — об авторах, чьи идентификаторы просто
      // не совпали с ключами карты. Снаружи эти случаи неразличимы.
      alert(t("st.err.noMapMatch"));
      return;
    }

    stCloseModal();
    const wantTab = ST_KIND_TAB[best[0]];
    if (tab !== wantTab) setTab(wantTab);
    stApplyHighlight(found[best[0]], d.title);
  } catch (e) {
    alert(e.message === "Failed to fetch" ? t("st.err.serverUnavailable") : e.message);
  } finally {
    btn.disabled = false; btn.textContent = was;
  }
}

// Ручное переключение вкладки перестраивает слои — подсветку снимаем.
document.addEventListener("DOMContentLoaded", () => {
  const t = document.getElementById("tab-toggle");
  if (t) t.addEventListener("click", stClearHighlight);
});

// ---------- recompute ----------

async function stRecompute() {
  if (_stBusy) return;
  _stBusy = true; _stError = "";
  renderStats();
  try {
    const r = await fetch("/api/stats", { method: "POST", cache: "no-store" });
    window.STATS = await stApiJson(r);
  } catch (e) {
    _stError = e.message === "Failed to fetch"
      ? t("st.staleAfterError")
      : e.message;
  } finally {
    _stBusy = false;
    renderStats();
  }
}

function renderStats() {
  const host = document.getElementById("stats-page");
  const s = window.STATS;

  if (!s) {
    host.innerHTML = `<div class="st-wrap"><div class="st-empty">
      ${t("st.notCollected")}
      <button class="st-btn" id="st-recompute">${t("st.compute")}</button>
      ${_stError ? `<div class="st-err">${esc(_stError)}</div>` : ""}
    </div></div>`;
    const btn = host.querySelector("#st-recompute");
    if (btn) btn.onclick = stRecompute;
    return;
  }

  const n = s.checks.length;
  const fail = s.checks.filter(c => c.status === "fail").length;
  const warn = s.checks.filter(c => c.status === "warn").length;

  host.innerHTML = `<div class="st-wrap">
    <div class="st-head">
      <div>
        <div class="st-title">${t("st.title")}</div>
        <div class="st-meta">${t("st.meta", esc(s.generated_at), stNum(s.totals.nodes), stNum(s.totals.rels), n)}</div>
      </div>
      <div class="st-head-right">
        <div class="st-verdict">
          <span class="st-v-n">${fail}</span><span class="st-v-k">${t("st.verdict.fail")}</span>
          <span class="st-v-n">${warn}</span><span class="st-v-k">${t("st.verdict.warn")}</span>
          <span class="st-v-n">${n - fail - warn}</span><span class="st-v-k">${t("st.verdict.ok")}</span>
        </div>
        <button class="st-btn" id="st-recompute" ${_stBusy ? "disabled" : ""}>${
          _stBusy ? t("st.recomputing") : t("st.recompute")}</button>
      </div>
    </div>
    ${_stError ? `<div class="st-err">${esc(_stError)}</div>` : ""}

    ${stTiles(s)}

    <div class="st-cols">
      ${stList(t("st.colNodes"), s.nodes, i => loc(i.label, i.label_en), i => loc(i.note, i.note_en) || "")}
      ${stList(t("st.colRels"), s.rels, i => i.type, i => loc(i.note, i.note_en) || "")}
    </div>
    <div class="st-cols">
      ${stList(t("st.colPubsByYear"), s.years, i => String(i.year), null)}
      ${stList(t("st.colTopDepts"), s.top_depts, i => loc(i.name, i.name_en), null)}
    </div>

    <div class="st-checks-h">${t("st.checksHeading")}</div>
    ${stChecks(s.checks)}
  </div>`;

  const recomputeBtn = host.querySelector("#st-recompute");
  if (recomputeBtn) recomputeBtn.onclick = stRecompute;
  host.querySelectorAll("[data-check]").forEach(el => {
    const open = () => stOpenExamples(el.getAttribute("data-check"));
    el.onclick = open;
    el.onkeydown = e => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
    };
  });
}
