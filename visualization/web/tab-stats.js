"use strict";

// Tab 5 — "Здоровье БД". Renders window.STATS, which the page loads as a
// static file; the "Пересчитать" button asks the server for a fresh snapshot
// straight from Neo4j and re-renders in place.

const ST_STATUS = {
  fail: { label: "проблема", cls: "st-fail" },
  warn: { label: "внимание", cls: "st-warn" },
  ok:   { label: "ок",       cls: "st-ok"   },
};
const ST_ORDER = { fail: 0, warn: 1, ok: 2 };

let _stBusy = false;
let _stError = "";

const stNum = n => (n == null ? "—" : n.toLocaleString("ru-RU").replace(/ /g, " "));

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
      return `<div class="st-check ${st.cls}">
        <span class="st-dot" aria-hidden="true"></span>
        <div class="st-check-body">
          <div class="st-check-top">
            <span class="st-check-t">${esc(c.title)}</span>
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
}
