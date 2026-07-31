"use strict";

function selectDept(did) {
  selected = null;
  map.getSource("sel-edges").setData(empty());
  map.getSource("sel-points").setData(empty());
  selectedDept = did;
  const partners = (DATA.dept_edges || []).filter(e => e.s === did || e.t === did);
  map.getSource("dept-focus").setData({
    type: "FeatureCollection",
    features: partners.map(e => {
      const oid = e.s === did ? e.t : e.s;
      const a = deptCentroid.get(did), b = deptCentroid.get(oid);
      if (!a || !b) return null;
      return { type: "Feature", properties: { w: e.w },
        geometry: { type: "LineString", coordinates: [proj(a[0], a[1]), proj(b[0], b[1])] } };
    }).filter(Boolean),
  });
  map.setPaintProperty("dept-edges", "line-opacity", 0);
  map.setPaintProperty("dept-fill",  "fill-opacity", ["case", ["==", ["get", "id"], did], 0.62, 0.08]);
  showDeptCard(did);
  drawOverlay();
}

function resetDeptFocus() {
  if (selectedDept === null) return;
  selectedDept = null;
  map.getSource("dept-focus").setData(empty());
  map.setPaintProperty("dept-edges", "line-opacity", DEPT_EDGE_OPACITY);
  map.setPaintProperty("dept-fill",  "fill-opacity", FILL_OPACITY);
}

function showAuthorProfile(key) {
  const n = nodeByKey.get(key);
  if (!n || n.kind !== "author") return;

  const pubs = (authorPubs.get(key) || []).map(k => nodeByKey.get(k)).filter(Boolean)
    .sort((a, b) => (b.year || 0) - (a.year || 0));
  const repos = (authorRepos.get(key) || []).map(k => nodeByKey.get(k)).filter(Boolean)
    .sort((a, b) => (b.stars || 0) - (a.stars || 0));

  const coauthMap = coauthMapOf(key);
  const topCoauths = [...coauthMap.entries()]
    .sort((a, b) => b[1] - a[1]).slice(0, 10)
    .map(([k, w]) => ({ node: nodeByKey.get(k), w })).filter(o => o.node);

  const years = pubs.map(p => p.year).filter(Boolean);
  const yearMin = years.length ? Math.min(...years) : null;
  const yearMax = years.length ? Math.max(...years) : null;
  const yearRange = yearMin ? (yearMin === yearMax ? `${yearMin}` : `${yearMin}-${yearMax}`) : "—";

  const deptObj   = deptById.get(n.dept);
  const deptColor = deptObj?.color || "#9aa2ac";
  const yearCounts = {};
  pubs.forEach(p => { if (p.year) yearCounts[p.year] = (yearCounts[p.year] || 0) + 1; });

  let html = `
    <span class="card-kind">автор</span>
    <h2 class="card-title">${esc(n.label)}</h2>
    <div class="profile-dept">
      <span class="dept-dot" style="background:${deptColor}"></span>
      ${esc(deptObj?.name || "Без департамента")}
    </div>
    ${n.degree ? `<div class="card-row"><b>Степень</b> ${esc(n.degree)}</div>` : ""}
    ${n.github ? `<div class="card-row"><b>GitHub</b> <a href="https://github.com/${esc(n.github)}" target="_blank">${esc(n.github)}</a></div>` : ""}
    <div class="stat-grid" style="margin-top:14px">
      <div class="stat"><div class="stat-num">${n.pubs_count || pubs.length}</div><div class="stat-lbl">публикаций</div></div>
      <div class="stat"><div class="stat-num">${coauthMap.size}</div><div class="stat-lbl">соавторов</div></div>
      <div class="stat"><div class="stat-num">${repos.length}</div><div class="stat-lbl">репозиториев</div></div>
      <div class="stat"><div class="stat-num">${yearRange}</div><div class="stat-lbl">активность</div></div>
    </div>`;

  if (years.length)
    html += `<div class="card-section">Публикации по годам</div>
      <svg id="pub-year-chart" class="profile-chart" width="100%" height="110"></svg>`;

  if (topCoauths.length)
    html += `<div class="card-section">Топ соавторы</div><ul class="card-list">` +
      topCoauths.map(({ node: c, w }) =>
        `<li data-k="${esc(c.key)}"><div class="li-name">${esc(c.label)}</div><span class="li-count">${w}</span></li>`
      ).join("") + `</ul>`;

  if (repos.length)
    html += `<div class="card-section">Репозитории <span class="tag gray">${repos.length}</span></div>
      <ul class="card-list">` +
      repos.map(r =>
        `<li data-k="${esc(r.key)}"><div class="li-name">${esc(r.label)}</div><span class="li-count">★${r.stars || 0}</span></li>`
      ).join("") + `</ul>`;

  if (pubs.length)
    html += `<div class="card-section">Последние публикации</div>
      <ul class="card-list">` +
      pubs.slice(0, 10).map(p =>
        `<li data-k="${esc(p.key)}">
          <div class="li-name">${esc(shortLabel(p.label || p.key))}</div>
          ${p.has_code ? '<span class="tag green" style="font-size:9px;padding:0 5px">код</span>' : ""}
          <span class="li-count">${p.year || "?"}</span>
        </li>`
      ).join("") + `</ul>`;

  html += `<button class="detail-profile-btn">Подробнее об авторе →</button>`;

  showDetail(html);

  if (years.length) {
    const svgEl = document.getElementById("pub-year-chart");
    if (svgEl) _drawYearChart(svgEl, yearCounts);
  }

  detail.querySelectorAll("li[data-k]").forEach(li => {
    li.onclick = () => {
      const k = li.getAttribute("data-k");
      const nd = nodeByKey.get(k); if (!nd) return;
      const targetTab = nd.kind === "repo" ? 2 : nd.kind === "pub" ? 3 : 1;
      if (targetTab !== tab) setTab(targetTab);
      selectNode(k);
      map.flyTo({ center: proj(...P(k)), zoom: Math.max(map.getZoom(), 8), duration: 700 });
    };
  });
  const profileBtn = detail.querySelector(".detail-profile-btn");
  if (profileBtn) profileBtn.onclick = () => { setTab(4); spShowAuthorProfile(key); };
}

function showDeptCard(did) {
  const d = deptById.get(did); if (!d) return;
  const partners = (DATA.dept_edges || [])
    .filter(e => e.s === did || e.t === did)
    .sort((a, b) => b.w - a.w);
  let html = `<div class="card-kind">департамент</div><div class="card-title">${esc(d.name)}</div>`;
  html += `<div class="card-row"><b>Авторов:</b> ${d.n_authors || 0}</div>`;
  html += `<div class="card-row"><b>Публикаций:</b> ${d.n_pubs || 0}</div>`;
  if (d.n_repos) html += `<div class="card-row"><b>Репозиториев:</b> ${d.n_repos}</div>`;
  html += `<div class="card-section">Сотрудничает с (${partners.length})</div><ul class="card-list">`;
  partners.slice(0, 15).forEach(e => {
    const oid = e.s === did ? e.t : e.s, o = deptById.get(oid); if (!o) return;
    html += `<li data-d="${oid}"><span class="li-name">${esc(o.name)}</span><span class="li-count">${e.w}</span></li>`;
  });
  html += `</ul>`;
  html += `<button class="detail-profile-btn">Подробнее о департаменте →</button>`;
  showDetail(html);
  detailBody.querySelectorAll("li[data-d]").forEach(li => {
    li.onclick = () => {
      const oid = +li.getAttribute("data-d");
      selectDept(oid);
      const c = deptCentroid.get(oid);
      if (c) map.easeTo({ center: proj(c[0], c[1]), duration: 500 });
    };
  });
  const deptProfileBtn = detailBody.querySelector(".detail-profile-btn");
  if (deptProfileBtn) deptProfileBtn.onclick = () => { setTab(4); spShowDeptProfile(did); };
}