"use strict";

// ---- Navigation (Tab 4) ----
// Browser history is the single source of truth (pushState in spShow*, popstate in main.js).
var spOnLanding = false;
// What's currently shown, so _onDetailReady (core.js) can re-render it once pub data arrives
var _spCurrentView = { kind: "landing" };

function spGoHome() { spShowLanding(); }

function _spRefreshCurrentView() {
  const v = _spCurrentView;
  if (v.kind === "author") spShowAuthorProfile(v.key);
  else if (v.kind === "pub") spShowPubProfile(v.key);
  else if (v.kind === "repo") spShowRepoProfile(v.key);
  else if (v.kind === "dept") spShowDeptProfile(v.id);
  else spShowLanding();
}

function _spNavBtns() {
  return `<div class="sp-nav-btns">
    <button class="sp-nav-btn sp-nav-back" onclick="history.back()">← Назад</button>
    <button class="sp-nav-btn" onclick="spGoHome()">На главную</button>
  </div>`;
}

// Authors and repos right away; publications after graph-search.js loads
const searchIndex = [
  ...DATA.authors.map(n => ({ key: n.key, kind: "author", label: n.label,
    ll: n.label.toLowerCase(),
    sub: `${deptById.get(n.dept)?.name || "—"} · ${n.pubs_count} публ.` })),
  ...DATA.repos.map(n => ({ key: n.key, kind: "repo", label: n.label,
    ll: (n.label + " " + (n.description || "")).toLowerCase(),
    sub: (n.url || "").replace("https://github.com/", "") })),
];
let pubSearchItems = [];
window._rebuildPubSearch = function() {
  pubSearchItems = DATA.pubs
    .filter(n => n.label)
    .map(n => ({ key: n.key, kind: "pub", label: n.label,
      ll: (n.label + " " + (n.journal || "")).toLowerCase(),
      sub: [n.year, deptById.get(n.dept)?.name, n.journal].filter(Boolean).join(" · ") }));
};

// Shared search used by both the sidebar (Tabs 1–3) and full-screen (Tab 4) variants
function searchHits(q, withDepts) {
  const tokens = q.split(/\s+/).filter(Boolean);
  const hits = [];
  for (const it of searchIndex) {
    if (tokens.every(t => it.ll.includes(t))) hits.push(it);
    if (hits.length > 600) break;
  }
  for (const it of pubSearchItems) {
    if (hits.length > 600) break;
    if (tokens.every(t => it.ll.includes(t))) hits.push(it);
  }
  if (withDepts) {
    for (const d of DATA.departments) {
      if (d.name === "Без департамента") continue;
      if (tokens.every(t => d.name.toLowerCase().includes(t)))
        hits.push({ key: d.id, kind: "dept", label: d.name, ll: d.name.toLowerCase(), sub: null });
    }
  }
  const ord = { author: 0, dept: 1, repo: 2, pub: 3 };
  hits.sort((a, b) => (ord[a.kind] ?? 9) - (ord[b.kind] ?? 9) || a.label.length - b.label.length);
  return hits.slice(0, 20);
}

// ---- Sidebar search (Tabs 1–3) ----

const searchInput   = document.getElementById("search");
const searchResults = document.getElementById("search-results");
let searchTimer = null;

searchInput.addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(runSearch, 120); });
searchInput.addEventListener("keydown", e => {
  if (e.key === "Escape") { searchInput.value = ""; searchResults.innerHTML = ""; searchInput.blur(); }
  else if (e.key === "Enter") { const f = searchResults.querySelector("li[data-k]"); if (f) f.click(); }
});

function runSearch() {
  const q = searchInput.value.trim().toLowerCase();
  if (q.length < 2) { searchResults.innerHTML = ""; return; }
  const top = searchHits(q, false);
  if (!top.length) { searchResults.innerHTML = '<div class="search-empty">ничего не найдено</div>'; return; }
  const kindLabel = { author: "автор", repo: "репо", pub: "публ." };
  const kindClass = { author: "",      repo: "blue",  pub: "gray" };
  searchResults.innerHTML = "<ul>" + top.map(h =>
    `<li data-k="${esc(h.key)}">` +
    `<div class="res-title"><span class="tag ${kindClass[h.kind]}">${kindLabel[h.kind]}</span> ${esc(h.label)}</div>` +
    (h.sub ? `<div class="search-sub">${esc(h.sub)}</div>` : "") +
    "</li>"
  ).join("") + "</ul>";
  searchResults.querySelectorAll("li[data-k]").forEach(li => {
    li.onclick = () => {
      const k = li.getAttribute("data-k");
      const n = nodeByKey.get(k); if (!n) return;
      const targetTab = n.kind === "repo" ? 2 : n.kind === "pub" ? 3 : 1;
      if (targetTab !== tab) setTab(targetTab);
      selectNode(k);
    };
  });
}

// ---- Full-screen search (Tab 4) ----

const spInput   = document.getElementById("sp-input");
const spResults = document.getElementById("sp-results");
let spTimer = null;

spInput.addEventListener("input", () => { clearTimeout(spTimer); spTimer = setTimeout(runSpSearch, 120); });
spInput.addEventListener("keydown", e => {
  if (e.key === "Escape") { spInput.value = ""; spResults.innerHTML = ""; spInput.blur(); }
  else if (e.key === "Enter") { spResults.querySelector("li[data-k]")?.click(); }
});

function runSpSearch() {
  const q = spInput.value.trim().toLowerCase();
  if (q.length < 2) { spResults.innerHTML = ""; return; }
  const top = searchHits(q, true);
  if (!top.length) { spResults.innerHTML = '<div class="search-empty" style="padding:10px 12px">ничего не найдено</div>'; return; }
  const kindLabel = { author: "автор", pub: "публикация", repo: "репо", dept: "департамент" };
  spResults.innerHTML = "<ul>" + top.map(h =>
    `<li data-k="${esc(h.key)}">
      <span class="sp-res-kind">${kindLabel[h.kind] || h.kind}</span>${esc(h.label)}
      ${h.sub ? `<div class="sp-res-sub">${esc(h.sub)}</div>` : ""}
    </li>`
  ).join("") + "</ul>";
  spResults.querySelectorAll("li[data-k]").forEach(li => {
    li.onclick = () => {
      const k = li.getAttribute("data-k");
      spResults.innerHTML = ""; spInput.value = ""; spInput.blur();
      const nd = nodeByKey.get(k);
      const dept = DATA.departments.find(d => d.id == k);
      if (nd?.kind === "author") { spShowAuthorProfile(k); return; }
      if (nd?.kind === "pub")    { spShowPubProfile(k);    return; }
      if (nd?.kind === "repo")   { spShowRepoProfile(k);   return; }
      if (dept)                  { spShowDeptProfile(dept.id); return; }
    };
  });
}

function spShowLanding() {
  spOnLanding = true;
  _spCurrentView = { kind: "landing" };
  document.title = "PAUK";
  if (typeof _replaceUrl === "function") _replaceUrl({ tab: 4 });
  const topAuthors = [...DATA.authors].sort((a, b) => b.pubs_count - a.pubs_count).slice(0, 12);

  const topPubs = [...pubAuthors.entries()]
    .sort((a, b) => b[1].length - a[1].length).slice(0, 10)
    .map(([k, au]) => ({ node: nodeByKey.get(k), count: au.length })).filter(o => o.node);

  const topDepts = [...DATA.departments]
    .filter(d => d.name !== "Без департамента" && (d.n_pubs || 0) > 0)
    .sort((a, b) => (b.n_pubs || 0) - (a.n_pubs || 0)).slice(0, 12);

  const spMain = document.getElementById("sp-main");
  spMain.innerHTML = `
    <div class="sp-landing">
      <div class="stat-grid" style="max-width:500px;margin:0 auto 24px">
        <div class="stat"><div class="stat-num">${DATA.authors.length.toLocaleString("ru-RU")}</div><div class="stat-lbl">авторов</div></div>
        <div class="stat"><div class="stat-num">${DATA.pubs.length.toLocaleString("ru-RU")}</div><div class="stat-lbl">публикаций</div></div>
        <div class="stat"><div class="stat-num">${DATA.repos.length}</div><div class="stat-lbl">репозиториев</div></div>
        <div class="stat"><div class="stat-num">${DATA.departments.filter(d => d.name !== "Без департамента").length}</div><div class="stat-lbl">департаментов</div></div>
      </div>
      <div class="sp-landing-hint">Топ авторов по публикациям</div>
      <div class="sp-top-chips">
        ${topAuthors.map(a => `<div class="sp-chip sp-chip-author" data-k="${esc(a.key)}">${esc(a.label)}</div>`).join("")}
      </div>
      <div class="sp-cols" style="margin-top:24px;text-align:left">
        <div class="sp-card">
          <div class="sp-card-title">Топ публикаций по числу авторов ИТМО</div>
          <ul>${topPubs.map(({ node: p, count }) =>
            `<li data-pub="${esc(p.key)}"><div class="li-name">${esc(shortLabel(p.label) || p.key)}</div><span class="li-count">${count}</span></li>`
          ).join("")}</ul>
        </div>
        <div class="sp-card">
          <div class="sp-card-title">Топ департаментов по публикациям</div>
          <ul>${topDepts.map(d =>
            `<li data-dept="${esc(d.id)}"><div class="li-name" style="display:flex;align-items:center;gap:7px"><span class="dept-dot" style="background:${d.color}"></span>${esc(d.name)}</div><span class="li-count">${d.n_pubs}</span></li>`
          ).join("")}</ul>
        </div>
      </div>
    </div>`;

  spMain.querySelectorAll(".sp-chip-author").forEach(el => {
    el.onclick = () => spShowAuthorProfile(el.getAttribute("data-k"));
  });
  spMain.querySelectorAll("li[data-pub]").forEach(li => {
    li.onclick = () => spShowPubProfile(li.getAttribute("data-pub"));
  });
  spMain.querySelectorAll("li[data-dept]").forEach(li => {
    li.onclick = () => spShowDeptProfile(+li.getAttribute("data-dept") || li.getAttribute("data-dept"));
  });
}

function _drawDeptPie(pubsByDept) {
  if (!pubsByDept.length) return "";
  const total = pubsByDept.reduce((s, d) => s + d.count, 0);
  if (!total) return "";
  const cx = 80, cy = 80, r = 72;
  let s = `<svg xmlns="http://www.w3.org/2000/svg" width="100%" viewBox="0 0 160 160" style="display:block;max-height:220px;margin:4px 0 0">`;
  if (pubsByDept.length === 1) {
    s += `<circle cx="${cx}" cy="${cy}" r="${r}" fill="${pubsByDept[0].color}" opacity="0.88"/>`;
  } else {
    let angle = -Math.PI / 2;
    pubsByDept.forEach(d => {
      const slice = (d.count / total) * 2 * Math.PI;
      if (slice < 0.001) return;
      const x1 = cx + r * Math.cos(angle), y1 = cy + r * Math.sin(angle);
      const x2 = cx + r * Math.cos(angle + slice), y2 = cy + r * Math.sin(angle + slice);
      const large = slice > Math.PI ? 1 : 0;
      s += `<path d="M${cx},${cy} L${x1.toFixed(2)},${y1.toFixed(2)} A${r},${r} 0 ${large} 1 ${x2.toFixed(2)},${y2.toFixed(2)} Z" fill="${d.color}" opacity="0.88"/>`;
      angle += slice;
    });
  }
  s += `</svg>`;
  return s;
}

function spShowAuthorProfile(key) {
  const n = nodeByKey.get(key);
  if (!n || n.kind !== "author") return;
  spOnLanding = false;
  _spCurrentView = { kind: "author", key };
  document.title = `${n.label} — PAUK`;
  if (typeof _pushUrl === "function") _pushUrl({ tab: 4, kind: "author", key });
  const pubs  = (authorPubs.get(key) || []).map(k => nodeByKey.get(k)).filter(Boolean)
    .sort((a, b) => (b.year || 0) - (a.year || 0));
  const repos = (authorRepos.get(key) || []).map(k => nodeByKey.get(k)).filter(Boolean)
    .sort((a, b) => (b.stars || 0) - (a.stars || 0));
  const coauthMap = coauthMapOf(key);
  const topCoauths = [...coauthMap.entries()].sort((a, b) => b[1] - a[1])
    .map(([k, w]) => ({ node: nodeByKey.get(k), w })).filter(o => o.node);
  const years = pubs.map(p => p.year).filter(Boolean);
  const yMin = years.length ? Math.min(...years) : 0, yMax = years.length ? Math.max(...years) : 0;
  const yearRange = years.length ? (yMin === yMax ? `${yMin}` : `${yMin}-${yMax}`) : "—";
  const deptObj = deptById.get(n.dept), deptColor = deptObj?.color || "#9aa2ac";
  const yearCounts = {};
  pubs.forEach(p => { if (p.year) yearCounts[p.year] = (yearCounts[p.year] || 0) + 1; });
  const activeYears = Object.keys(yearCounts).map(Number).sort((a, b) => a - b);

  // dept distribution for pie
  const deptPubCounts = new Map();
  pubs.forEach(p => {
    const did = p.dept || "__none__";
    deptPubCounts.set(did, (deptPubCounts.get(did) || 0) + 1);
  });
  const sortedDepts = [...deptPubCounts.entries()].sort((a, b) => b[1] - a[1]);
  const othersCount = sortedDepts.slice(6).reduce((s, [, c]) => s + c, 0);
  const pubsByDept = sortedDepts.slice(0, 6).map(([id, count]) => {
    if (id === "__none__") return { id: 0, name: "Без отдела", color: "#b0b8c4", count };
    const dobj = deptById.get(id);
    return { id, name: dobj?.name || "Неизв.", color: dobj?.color || "#9aa2ac", count };
  });
  if (othersCount > 0) pubsByDept.push({ id: -1, name: "Прочие", color: "#c4cad4", count: othersCount });

  const spMain = document.getElementById("sp-main");
  spMain.innerHTML = `
    ${_spNavBtns()}
    <div class="sp-profile">
      <div class="sp-profile-header">
        <span class="card-kind">автор</span>
        <div class="sp-profile-name">${esc(n.label)}</div>
        <div class="profile-dept sp-dept-link" data-dept-id="${n.dept}" style="cursor:pointer" title="Открыть профиль департамента">
          <span class="dept-dot" style="background:${deptColor}"></span>
          ${esc(deptObj?.name || "Без департамента")}
          <span style="font-size:10px;color:var(--faint);margin-left:4px">· последний по публикациям</span>
        </div>
        ${n.degree ? `<div class="card-row" style="margin-top:4px"><b>Степень</b> ${esc(n.degree)}</div>` : ""}
        ${n.github ? `<div class="card-row"><b>GitHub</b> <a href="https://github.com/${esc(n.github)}" target="_blank">${esc(n.github)}</a></div>` : ""}
        <div class="stat-grid" style="margin-top:16px;grid-template-columns:repeat(4,1fr)">
          <div class="stat"><div class="stat-num">${n.pubs_count || pubs.length}</div><div class="stat-lbl">публикаций</div></div>
          <div class="stat"><div class="stat-num">${coauthMap.size}</div><div class="stat-lbl">соавторов</div></div>
          <div class="stat"><div class="stat-num">${repos.length}</div><div class="stat-lbl">репозиториев</div></div>
          <div class="stat"><div class="stat-num">${yearRange}</div><div class="stat-lbl">активность</div></div>
        </div>
      </div>
      ${years.length ? `<div class="sp-card"><div class="sp-card-title">Публикации по годам</div><svg id="sp-year-chart" width="100%" height="120"></svg></div>` : ""}
      <div class="sp-cols">
        ${topCoauths.length ? `<div class="sp-card"><div class="sp-card-title">Соавторы (${coauthMap.size})</div><ul>${topCoauths.map(({ node: c, w }) =>
          `<li data-k="${esc(c.key)}"><div class="li-name">${esc(c.label)}</div><span class="li-count">${w}</span></li>`
        ).join("")}</ul></div>` : ""}
        ${pubsByDept.length >= 1 ? `<div class="sp-card"><div class="sp-card-title">Связь с департаментами</div>${_drawDeptPie(pubsByDept)}<div class="sp-pie-legend">${pubsByDept.map(d => `<span class="sp-pie-legend-item"><span class="sp-pie-legend-dot" style="background:${d.color}"></span><span>${esc(d.name)}</span><span class="li-count" style="font-size:10px;padding:1px 5px">${d.count}</span></span>`).join("")}</div></div>` : ""}
      </div>
      ${repos.length ? `<div class="sp-card"><div class="sp-card-title">Репозитории (${repos.length})</div><ul>${repos.map(r =>
        `<li data-k="${esc(r.key)}"><div class="li-name">${esc(r.label)}</div><span class="li-count">★${r.stars || 0}</span></li>`
      ).join("")}</ul></div>` : ""}
      ${pubs.length ? `<div class="sp-card"><div class="sp-card-title">Все публикации (${pubs.length})</div><ul>${pubs.map(p =>
        `<li data-k="${esc(p.key)}">
          <div class="li-name">${esc(shortLabel(p.label))}${p.dept != null ? `<div style="font-size:10px;color:var(--muted);margin-top:1px">${esc(deptById.get(p.dept)?.name || "")}</div>` : ""}</div>
          ${p.has_code ? '<span class="tag green" style="font-size:9px;padding:0 5px">код</span>' : ""}
          <span class="li-count">${p.year || "?"}</span>
        </li>`
      ).join("")}</ul></div>` : ""}
    </div>`;

  const svgEl = document.getElementById("sp-year-chart");
  if (svgEl && years.length) _drawYearChart(svgEl, yearCounts);

  spMain.querySelectorAll("[data-dept-id]").forEach(el => {
    el.onclick = () => {
      const v = el.getAttribute("data-dept-id");
      if (v !== null && v !== "" && v !== "undefined") spShowDeptProfile(+v);
    };
  });
  spMain.querySelectorAll("li[data-k]").forEach(li => {
    li.onclick = () => {
      const k = li.getAttribute("data-k"); const nd = nodeByKey.get(k); if (!nd) return;
      if (nd.kind === "author") { spShowAuthorProfile(k); return; }
      if (nd.kind === "pub")    { spShowPubProfile(k);    return; }
      if (nd.kind === "repo")   { spShowRepoProfile(k);   return; }
    };
  });
}

function spShowPubProfile(key) {
  const n = nodeByKey.get(key);
  if (!n || n.kind !== "pub") return;
  spOnLanding = false;
  _spCurrentView = { kind: "pub", key };
  document.title = `${n.label || n.key} — PAUK`;
  if (typeof _pushUrl === "function") _pushUrl({ tab: 4, kind: "pub", key });
  const authors = (pubAuthors.get(key) || []).map(k => nodeByKey.get(k)).filter(Boolean);
  // similarity: shared ITMO authors (weight×3) + same journal (3) + same dept (1)
  const simScores = new Map();
  (pubAdj.get(key) || []).forEach(({ o, w }) => simScores.set(o, (simScores.get(o) || 0) + w * 3));
  DATA.pubs.forEach(p => {
    if (p.key === key) return;
    let sc = 0;
    if (n.journal && p.journal === n.journal) sc += 3;
    if (n.dept && p.dept === n.dept) sc += 1;
    if (sc > 0) simScores.set(p.key, (simScores.get(p.key) || 0) + sc);
  });
  const relPubs = [...simScores.entries()]
    .sort((a, b) => b[1] - a[1]).slice(0, 10)
    .map(([k]) => nodeByKey.get(k)).filter(Boolean);
  const urls = Array.isArray(n.code_url) ? n.code_url : [];
  const deptObj = deptById.get(n.dept), deptColor = deptObj?.color || "#9aa2ac";

  const spMain = document.getElementById("sp-main");
  spMain.innerHTML = `
    ${_spNavBtns()}
    <div class="sp-profile">
      <div class="sp-profile-header">
        <span class="card-kind">публикация</span>
        <div class="sp-profile-name">${esc(n.label || n.key)}</div>
        ${deptObj ? `<div class="profile-dept sp-dept-link" data-dept-id="${n.dept}" style="cursor:pointer" title="Открыть профиль департамента"><span class="dept-dot" style="background:${deptColor}"></span>${esc(deptObj.name)}</div>` : ""}
        ${n.journal ? `<div class="card-row" style="margin-top:6px"><b>Журнал</b> ${esc(n.journal)}</div>` : ""}
        ${n.doi ? `<div class="card-row"><b>DOI</b> <a href="https://doi.org/${esc(n.doi.replace(/^https?:\/\/doi\.org\//, ""))}" target="_blank">${esc(n.doi)}</a></div>` : ""}
        ${n.has_code && urls.length ? `<div class="card-row"><b>Код</b> ${urls.map(u => `<a href="${esc(u)}" target="_blank">${esc(u.replace("https://github.com/",""))}</a>`).join(", ")}</div>` : ""}
        <div class="stat-grid" style="margin-top:16px">
          <div class="stat"><div class="stat-num">${authors.length}</div><div class="stat-lbl">авторов ИТМО</div></div>
          <div class="stat"><div class="stat-num">${n.year || "?"}</div><div class="stat-lbl">год</div></div>
        </div>
      </div>
      <div class="sp-cols">
        <div class="sp-card"><div class="sp-card-title">Авторы ИТМО (${authors.length})</div><ul>${authors.map(a =>
          `<li data-k="${esc(a.key)}"><div class="li-name">${esc(a.label)}</div></li>`
        ).join("")}</ul></div>
        ${relPubs.length ? `<div class="sp-card"><div class="sp-card-title">Похожие публикации</div><ul>${relPubs.map(p =>
          `<li data-k="${esc(p.key)}"><div class="li-name">${esc(shortLabel(p.label))}</div><span class="li-count">${p.year || "?"}</span></li>`
        ).join("")}</ul></div>` : ""}
      </div>
    </div>`;

  spMain.querySelectorAll("[data-dept-id]").forEach(el => {
    el.onclick = () => {
      const v = el.getAttribute("data-dept-id");
      if (v !== null && v !== "" && v !== "undefined") spShowDeptProfile(+v);
    };
  });
  spMain.querySelectorAll("li[data-k]").forEach(li => {
    li.onclick = () => {
      const k = li.getAttribute("data-k"); const nd = nodeByKey.get(k); if (!nd) return;
      if (nd.kind === "author") { spShowAuthorProfile(k); return; }
      if (nd.kind === "pub")    { spShowPubProfile(k);    return; }
    };
  });
}

function spShowRepoProfile(key) {
  const n = nodeByKey.get(key);
  if (!n || n.kind !== "repo") return;
  spOnLanding = false;
  _spCurrentView = { kind: "repo", key };
  document.title = `${n.label} — PAUK`;
  if (typeof _pushUrl === "function") _pushUrl({ tab: 4, kind: "repo", key });
  const persons = (repoPersons.get(key) || []).map(p => ({ ...p, node: nodeByKey.get(p.key) })).filter(p => p.node);
  const pubs    = (repoPubs.get(key) || []).map(k => nodeByKey.get(k)).filter(Boolean)
    .sort((a, b) => (b.year || 0) - (a.year || 0));

  const spMain = document.getElementById("sp-main");
  spMain.innerHTML = `
    ${_spNavBtns()}
    <div class="sp-profile">
      <div class="sp-profile-header">
        <span class="card-kind">репозиторий</span>
        <div class="sp-profile-name">${esc(n.label)}</div>
        <div class="profile-dept"><span class="dept-dot" style="background:${deptById.get(n.dept)?.color || "#9aa2ac"}"></span>${esc(deptById.get(n.dept)?.name || "Без департамента")}</div>
        ${n.stars ? `<div class="card-row"><b>Звёзды</b> ★ ${n.stars}</div>` : ""}
        ${n.description ? `<div class="card-row">${esc(n.description)}</div>` : ""}
        ${n.url ? `<div class="card-row"><a href="${esc(n.url)}" target="_blank">${esc(n.url.replace("https://github.com/",""))}</a></div>` : ""}
      </div>
      <div class="sp-cols">
        ${persons.length ? `<div class="sp-card"><div class="sp-card-title">Участники ИТМО (${persons.length})</div><ul>${persons.map(p =>
          `<li data-k="${esc(p.key)}">${esc(p.node.label)} <span class="tag gray">${esc(p.role)}</span></li>`
        ).join("")}</ul></div>` : ""}
        ${pubs.length ? `<div class="sp-card"><div class="sp-card-title">Публикации (${pubs.length})</div><ul>${pubs.slice(0,15).map(p =>
          `<li data-k="${esc(p.key)}"><div class="li-name">${esc(shortLabel(p.label))}</div><span class="li-count">${p.year || "?"}</span></li>`
        ).join("")}</ul></div>` : ""}
      </div>
    </div>`;

  spMain.querySelectorAll("li[data-k]").forEach(li => {
    li.onclick = () => {
      const k = li.getAttribute("data-k"); const nd = nodeByKey.get(k); if (!nd) return;
      if (nd.kind === "author") { spShowAuthorProfile(k); return; }
      if (nd.kind === "pub")    { spShowPubProfile(k);    return; }
    };
  });
}

function spShowDeptProfile(id) {
  const d = deptById.get(id) || deptById.get(+id);
  if (!d) return;
  spOnLanding = false;
  _spCurrentView = { kind: "dept", id: d.id };
  document.title = `${d.name} — PAUK`;
  if (typeof _pushUrl === "function") _pushUrl({ tab: 4, kind: "dept", id: d.id });
  const deptAuthors = DATA.authors.filter(a => a.dept == id)
    .sort((a, b) => b.pubs_count - a.pubs_count);

  // Publications from all dept authors
  const deptPubKeys = new Set();
  deptAuthors.forEach(a => (authorPubs.get(a.key) || []).forEach(pk => deptPubKeys.add(pk)));
  const deptPubs = [...deptPubKeys].map(k => nodeByKey.get(k)).filter(Boolean)
    .sort((a, b) => (b.year || 0) - (a.year || 0));
  const yearCounts = {};
  deptPubs.forEach(p => { if (p.year) yearCounts[p.year] = (yearCounts[p.year] || 0) + 1; });

  const years = deptPubs.map(p => p.year).filter(Boolean);
  const yMin = years.length ? Math.min(...years) : 0, yMax = years.length ? Math.max(...years) : 0;
  const yearRange = years.length ? (yMin === yMax ? `${yMin}` : `${yMin}-${yMax}`) : "—";

  // Count repos
  const deptRepoKeys = new Set();
  deptAuthors.forEach(a => (authorRepos.get(a.key) || []).forEach(rk => deptRepoKeys.add(rk)));

  const partners = (DATA.dept_edges || []).filter(e => e.s == id || e.t == id)
    .sort((a, b) => b.w - a.w).slice(0, 12)
    .map(e => {
      const oid = e.s == id ? e.t : e.s;
      const od = deptById.get(oid); if (!od) return null;
      return { dept: od, w: e.w };
    }).filter(Boolean);

  const spMain = document.getElementById("sp-main");
  spMain.innerHTML = `
    ${_spNavBtns()}
    <div class="sp-profile">
      <div class="sp-profile-header">
        <span class="card-kind">департамент</span>
        <div class="sp-profile-name" style="display:flex;align-items:center;gap:8px">
          <span class="dept-dot" style="background:${d.color || "#9aa2ac"};width:12px;height:12px"></span>
          ${esc(d.name)}
        </div>
        <div class="stat-grid" style="margin-top:16px;grid-template-columns:repeat(4,1fr)">
          <div class="stat"><div class="stat-num">${deptAuthors.length}</div><div class="stat-lbl">авторов</div></div>
          <div class="stat"><div class="stat-num">${deptPubKeys.size}</div><div class="stat-lbl">публикаций</div></div>
          <div class="stat"><div class="stat-num">${deptRepoKeys.size}</div><div class="stat-lbl">репозиториев</div></div>
          <div class="stat"><div class="stat-num">${yearRange}</div><div class="stat-lbl">активность</div></div>
        </div>
      </div>
      ${years.length ? `<div class="sp-card"><div class="sp-card-title">Публикации по годам</div><svg id="sp-dept-year-chart" width="100%" height="120"></svg></div>` : ""}
      <div class="sp-cols">
        <div class="sp-card"><div class="sp-card-title">Топ авторов (${deptAuthors.length})</div><ul>${deptAuthors.slice(0,20).map(a =>
          `<li data-k="${esc(a.key)}"><div class="li-name">${esc(a.label)}</div><span class="li-count">${a.pubs_count}</span></li>`
        ).join("")}</ul></div>
        ${partners.length ? `<div class="sp-card"><div class="sp-card-title">Связанные департаменты (${partners.length})</div><ul>${partners.map(({ dept: od, w }) =>
          `<li data-dept="${esc(od.id)}"><div class="li-name" style="color:${od.color}">${esc(od.name)}</div><span class="li-count">${w}</span></li>`
        ).join("")}</ul></div>` : ""}
      </div>
      ${deptPubs.length ? `<div class="sp-card"><div class="sp-card-title">Все публикации (${deptPubs.length})</div><ul>${deptPubs.map(p =>
        `<li data-k="${esc(p.key)}">
          <div class="li-name">${esc(shortLabel(p.label || p.key))}</div>
          ${p.has_code ? '<span class="tag green" style="font-size:9px;padding:0 5px">код</span>' : ""}
          <span class="li-count">${p.year || "?"}</span>
        </li>`
      ).join("")}</ul></div>` : ""}
    </div>`;

  const svgEl = document.getElementById("sp-dept-year-chart");
  if (svgEl && years.length) _drawYearChart(svgEl, yearCounts);

  spMain.querySelectorAll("li[data-k]").forEach(li => {
    li.onclick = () => {
      const k = li.getAttribute("data-k");
      const nd = nodeByKey.get(k); if (!nd) return;
      if (nd.kind === "author") { spShowAuthorProfile(k); return; }
      if (nd.kind === "pub")    { spShowPubProfile(k);    return; }
      if (nd.kind === "repo")   { spShowRepoProfile(k);   return; }
    };
  });
  spMain.querySelectorAll("li[data-dept]").forEach(li => {
    li.onclick = () => { const did = li.getAttribute("data-dept"); if (did) spShowDeptProfile(+did || did); };
  });
}