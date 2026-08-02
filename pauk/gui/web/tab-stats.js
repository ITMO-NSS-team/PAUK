"use strict";

// Tab 5 — "Здоровье БД". Renders window.STATS, which the page loads as a
// static file; the "Пересчитать" button asks the server for a fresh snapshot
// straight from Neo4j and re-renders in place. Clicking a check opens a popup
// with the rows behind it, which can also be saved as CSV.

const ST_STATUS = {
  fail: { label: "проблема", cls: "st-fail" },
  warn: { label: "внимание", cls: "st-warn" },
  ok:   { label: "ок",       cls: "st-ok"   },
};
const ST_ORDER = { fail: 0, warn: 1, ok: 2 };

const ST_VIEW_LIMIT = 300;    // строк в окне
const ST_CSV_LIMIT  = 5000;   // строк в выгрузке

let _stBusy = false;
let _stError = "";

const stNum = n => (n == null ? "—" : n.toLocaleString("ru-RU").replace(/ /g, " "));

// Списки приходят массивами (например, идентификаторы группы дублей).
const stCell = v => Array.isArray(v) ? v.join(", ") : (v == null ? "" : String(v));

function stTiles(s) {
  const by = l => (s.nodes.find(n => n.label === l) || {}).n;
  const tiles = [
    ["публикаций",       by("Публикации")],
    ["сотрудников ИТМО", by("— сотрудники ИТМО")],
    ["репозиториев",     by("Репозитории")],
    ["департаментов",    by("Департаменты")],
    ["связей",           s.totals.rels],
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
    if (!g) groups.push(g = { name: c.group, items: [] });
    g.items.push(c);
  });

  return groups.map(g => {
    const items = g.items.slice().sort((a, b) =>
      ST_ORDER[a.status] - ST_ORDER[b.status] || (b.pct || 0) - (a.pct || 0) || b.n - a.n);
    return `<div class="st-block"><div class="st-col-h">${esc(g.name)}</div>` + items.map(c => {
      const st = ST_STATUS[c.status];
      const clickable = c.has_examples && c.n > 0;
      return `<div class="st-check ${st.cls}${clickable ? " st-clickable" : ""}"${
        clickable ? ` data-check="${esc(c.id)}" tabindex="0" role="button"` : ""}>
        <span class="st-dot" aria-hidden="true"></span>
        <div class="st-check-body">
          <div class="st-check-top">
            <span class="st-check-t">${esc(c.title)}${
              clickable ? `<span class="st-check-more">примеры</span>` : ""}</span>
            <span class="st-check-v">${stNum(c.n)}${
              c.pct != null ? ` <span class="st-check-pct">${c.pct}%</span>` : ""
            }</span>
          </div>
          ${c.hint ? `<div class="st-check-hint">${esc(c.hint)}</div>` : ""}
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
          <button class="st-btn" data-act="map">Выделить на карте</button>
          <button class="st-btn" data-act="csv">Скачать CSV</button>
          <button class="st-btn st-modal-x" data-act="close" aria-label="Закрыть">×</button>
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

async function stFetchExamples(id, limit) {
  const r = await fetch(`/api/check?id=${encodeURIComponent(id)}&limit=${limit}`,
                        { cache: "no-store" });
  const body = await r.json();
  if (!r.ok) throw new Error(body.error || "Сервер вернул ошибку");
  return body;
}

async function stOpenExamples(id) {
  const m = stModalEl();
  m.classList.add("visible");
  document.addEventListener("keydown", stEscClose);
  m.querySelector(".st-modal-title").textContent = "Примеры";
  m.querySelector(".st-modal-sub").textContent = "";
  m.querySelector(".st-modal-body").innerHTML = `<div class="st-modal-msg">Загружаем…</div>`;
  m.querySelector('[data-act="csv"]').onclick = () => stDownloadCsv(id);
  m.querySelector('[data-act="map"]').onclick = () => stHighlightOnMap(id);

  let d;
  try {
    d = await stFetchExamples(id, ST_VIEW_LIMIT);
  } catch (e) {
    m.querySelector(".st-modal-body").innerHTML =
      `<div class="st-modal-msg">${esc(e.message === "Failed to fetch"
        ? "Сервер недоступен." : e.message)}</div>`;
    return;
  }

  m.querySelector(".st-modal-title").textContent = d.title;
  m.querySelector(".st-modal-sub").textContent =
    (d.hint ? d.hint + " · " : "") +
    (d.truncated ? `показаны первые ${stNum(d.shown)} из ${stNum(d.total)}`
                 : `записей: ${stNum(d.total)}`);

  if (!d.rows.length) {
    m.querySelector(".st-modal-body").innerHTML =
      `<div class="st-modal-msg">Ничего не найдено.</div>`;
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
  btn.disabled = true; btn.textContent = "Готовим…";
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
    alert(e.message === "Failed to fetch" ? "Сервер недоступен." : e.message);
  } finally {
    btn.disabled = false; btn.textContent = was;
  }
}

// ---------- highlighting the offending entities on the map ----------

// Red is a deliberate exception to the grayscale rule: on the map colour
// already carries meaning, and the point here is to stand out against it.
const ST_HL_COLOR = "#e03131";
const ST_HL_DIM   = 0.05;
const ST_HL_LAYERS = ["st-problem-edges", "st-problem-nodes"];

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
  if (map.getLayer("authors")) map.setPaintProperty("authors", "circle-opacity", NODE_OPACITY);
  if (map.getLayer("repos"))
    map.setPaintProperty("repos", "circle-opacity", tab === 2 ? 1 : NODE_OPACITY);
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

  if (map.getLayer("authors")) map.setPaintProperty("authors", "circle-opacity", ST_HL_DIM);
  if (map.getLayer("repos"))   map.setPaintProperty("repos", "circle-opacity", ST_HL_DIM);
  if (map.getLayer("pubs"))    map.setPaintProperty("pubs", "icon-opacity", ST_HL_DIM);
  if (map.getLayer("edges"))   map.setPaintProperty("edges", "line-opacity", 0.03);
  if (map.getLayer("dept-fill")) map.setPaintProperty("dept-fill", "fill-opacity", 0.04);
  if (map.getLayer("dept-line")) map.setPaintProperty("dept-line", "line-opacity", 0.1);
  if (map.getLayer("dept-edges")) map.setPaintProperty("dept-edges", "line-opacity", 0.04);

  ST_HL_LAYERS.forEach(id => { if (map.getLayer(id)) map.removeLayer(id); });

  map.addLayer({
    id: "st-problem-edges", type: "line", source: "edges",
    filter: ["any", ["in", ["get", "s"], lit], ["in", ["get", "t"], lit]],
    paint: { "line-color": ST_HL_COLOR, "line-width": 1.2, "line-opacity": 0.55 },
  });
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
      <button class="st-btn" data-act="reset">Сбросить</button>`;
    document.body.appendChild(b);
    b.querySelector('[data-act="reset"]').onclick = stClearHighlight;
  }
  b.querySelector(".st-hl-text").textContent = `${title} — выделено ${stNum(n)}`;
  b.classList.add("visible");
}

async function stHighlightOnMap(id, title) {
  const m = stModalEl();
  const btn = m.querySelector('[data-act="map"]');
  const was = btn.textContent;
  btn.disabled = true; btn.textContent = "Ищем…";
  try {
    const d = await stFetchExamples(id, ST_CSV_LIMIT);
    const found = stKeysFromRows(d.rows);
    const best = ["author", "pub", "repo"]
      .map(k => [k, found[k].length]).sort((a, b) => b[1] - a[1])[0];

    if (!best[1]) {
      alert("В этой проверке нет объектов, которые есть на карте — " +
            "она о департаментах, ссылках или связях.");
      return;
    }

    stCloseModal();
    const wantTab = ST_KIND_TAB[best[0]];
    if (tab !== wantTab) setTab(wantTab);
    stApplyHighlight(found[best[0]], d.title);
  } catch (e) {
    alert(e.message === "Failed to fetch" ? "Сервер недоступен." : e.message);
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
    const body = await r.json();
    if (!r.ok) throw new Error(body.error || "Сервер вернул ошибку");
    window.STATS = body;
  } catch (e) {
    _stError = e.message === "Failed to fetch"
      ? "Сервер недоступен — показаны сохранённые числа."
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
      Данные ещё не собраны.
      <button class="st-btn" id="st-recompute">Посчитать</button>
      ${_stError ? `<div class="st-err">${esc(_stError)}</div>` : ""}
    </div></div>`;
    host.querySelector("#st-recompute").onclick = stRecompute;
    return;
  }

  const n = s.checks.length;
  const fail = s.checks.filter(c => c.status === "fail").length;
  const warn = s.checks.filter(c => c.status === "warn").length;

  host.innerHTML = `<div class="st-wrap">
    <div class="st-head">
      <div>
        <div class="st-title">Здоровье базы</div>
        <div class="st-meta">данные на ${esc(s.generated_at)} ·
          ${stNum(s.totals.nodes)} узлов, ${stNum(s.totals.rels)} связей ·
          ${n} проверок</div>
      </div>
      <div class="st-head-right">
        <div class="st-verdict">
          <span class="st-v-n">${fail}</span><span class="st-v-k">проблем</span>
          <span class="st-v-n">${warn}</span><span class="st-v-k">замечаний</span>
          <span class="st-v-n">${n - fail - warn}</span><span class="st-v-k">чисто</span>
        </div>
        <button class="st-btn" id="st-recompute" ${_stBusy ? "disabled" : ""}>${
          _stBusy ? "Считаем…" : "Пересчитать"}</button>
      </div>
    </div>
    ${_stError ? `<div class="st-err">${esc(_stError)}</div>` : ""}

    ${stTiles(s)}

    <div class="st-cols">
      ${stList("Узлы", s.nodes, i => i.label, i => i.note || "")}
      ${stList("Связи", s.rels, i => i.type, i => i.note || "")}
    </div>
    <div class="st-cols">
      ${stList("Публикации по годам", s.years, i => String(i.year), null)}
      ${stList("Крупнейшие департаменты", s.top_depts, i => i.name, null)}
    </div>

    <div class="st-checks-h">Проверки целостности</div>
    ${stChecks(s.checks)}
  </div>`;

  host.querySelector("#st-recompute").onclick = stRecompute;
  host.querySelectorAll("[data-check]").forEach(el => {
    const open = () => stOpenExamples(el.getAttribute("data-check"));
    el.onclick = open;
    el.onkeydown = e => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
    };
  });
}
