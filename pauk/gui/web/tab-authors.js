"use strict";

// ---------- node icon ----------

// PAD reserves room for the shadow blur; pixelRatio 4 below keeps the
// raster crisp at the largest icon-size multipliers.
const CIRCLE_IMG_SIZE = 56, CIRCLE_IMG_PAD = 14, CIRCLE_IMG_FULL = CIRCLE_IMG_SIZE + CIRCLE_IMG_PAD * 2;
function circleImg(color) {
  const cv = document.createElement("canvas"); cv.width = cv.height = CIRCLE_IMG_FULL;
  const g = cv.getContext("2d");
  const cx = CIRCLE_IMG_FULL / 2, cy = cx, r = CIRCLE_IMG_SIZE / 2;
  g.shadowColor = "rgba(0,0,0,0.24)";
  g.shadowBlur = CIRCLE_IMG_PAD * 0.85;
  g.shadowOffsetY = CIRCLE_IMG_PAD * 0.12;
  g.beginPath();
  g.arc(cx, cy, r, 0, Math.PI * 2);
  const [h, s, l] = hexToHsl(color);
  const grad = g.createRadialGradient(cx - r * 0.3, cy - r * 0.35, r * 0.2, cx, cy, r * 1.1);
  grad.addColorStop(0, hslToHex(h, s, Math.min(1, l + 0.1)));
  grad.addColorStop(1, color);
  g.fillStyle = grad;
  g.fill();
  g.shadowColor = "transparent";
  g.lineWidth = 2;
  g.strokeStyle = hslToHex(h, s, Math.max(0, l - 0.14));
  g.stroke();
  return g.getImageData(0, 0, CIRCLE_IMG_FULL, CIRCLE_IMG_FULL);
}
function ensureCircleImage(color) {
  const id = "ci" + color.replace("#", "");
  if (!map.hasImage(id)) map.addImage(id, circleImg(color), { pixelRatio: 4 });
  return id;
}

// ---------- department selection ----------

function selectDept(did) {
  selected = null;
  map.getSource("sel-edges").setData(empty());
  map.getSource("sel-points").setData(empty());
  selectedDept = did;
  const c = deptCentroid.get(did);
  if (c && map.getZoom() < DEPT_CLICK_ZOOM)
    map.flyTo({ center: proj(c[0], c[1]), zoom: DEPT_CLICK_ZOOM - 0.3, duration: 700 });
  map.setPaintProperty("dept-fill", "fill-opacity", ["case", ["==", ["get", "id"], did], 0.62, 0.08]);
  showDeptCard(did);
  drawOverlay();
}

function resetDeptFocus() {
  if (selectedDept === null) return;
  selectedDept = null;
  map.setPaintProperty("dept-fill", "fill-opacity", FILL_OPACITY);
}

// ---------- author profile ----------

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

  const variants = n.name_variants || [];
  let html = `
    <span class="card-kind">${t("author.kind")}</span>
    <h2 class="card-title">${esc(authorDisplayName(n))}</h2>
    ${LANG !== "en" && n.name_en && n.name_en !== n.label ? `<div class="card-subtitle">${esc(n.name_en)}</div>` : ""}
    ${variants.length ? `<details class="name-variants">
      <summary>${t("author.otherSpellings")}</summary>
      <ul>${variants.map(v => `<li>${esc(v)}</li>`).join("")}</ul>
    </details>` : ""}
    <div class="profile-dept">
      <span class="dept-dot" style="background:${deptColor}"></span>
      ${esc(deptDisplayName(deptObj) || t("common.noDept"))}
    </div>
    ${n.degree ? `<div class="card-row"><b>${t("author.degree")}</b> ${esc(n.degree)}</div>` : ""}
    ${n.github ? `<div class="card-row"><b>GitHub</b> <a href="https://github.com/${esc(n.github)}" target="_blank">${esc(n.github)}</a></div>` : ""}
    ${n.orcid ? `<div class="card-row"><b>ORCID</b> <a href="https://orcid.org/${esc(n.orcid)}" target="_blank">${esc(n.orcid)}</a></div>` : ""}
    <div class="stat-grid" style="margin-top:14px">
      <div class="stat"><div class="stat-num">${n.pubs_count || pubs.length}</div><div class="stat-lbl">${t("overview.pubs")}</div></div>
      <div class="stat"><div class="stat-num">${coauthMap.size}</div><div class="stat-lbl">${t("author.coauthors")}</div></div>
      <div class="stat"><div class="stat-num">${repos.length}</div><div class="stat-lbl">${t("overview.repos")}</div></div>
      <div class="stat"><div class="stat-num">${yearRange}</div><div class="stat-lbl">${t("author.activity")}</div></div>
    </div>`;

  if (years.length)
    html += `<div class="card-section">${t("author.pubsByYear")}</div>
      <svg id="pub-year-chart" class="profile-chart" width="100%" height="110"></svg>`;

  if (topCoauths.length)
    html += `<div class="card-section">${t("author.topCoauthors")}</div><ul class="card-list">` +
      topCoauths.map(({ node: c, w }) =>
        `<li data-k="${esc(c.key)}"><div class="li-name">${esc(authorDisplayName(c))}</div><span class="li-count">${w}</span></li>`
      ).join("") + `</ul>`;

  if (repos.length)
    html += `<div class="card-section">${t("tab.repos")} <span class="tag gray">${repos.length}</span></div>
      <ul class="card-list">` +
      repos.map(r =>
        `<li data-k="${esc(r.key)}"><div class="li-name">${esc(r.label)}</div><span class="li-count">★${r.stars || 0}</span></li>`
      ).join("") + `</ul>`;

  if (pubs.length)
    html += `<div class="card-section">${t("author.recentPubs")}</div>
      <ul class="card-list">` +
      pubs.slice(0, 10).map(p =>
        `<li data-k="${esc(p.key)}">
          <div class="li-name">${esc(shortLabel(p.label || p.key))}</div>
          ${p.has_code ? `<span class="tag green" style="font-size:9px;padding:0 5px">${t("common.code")}</span>` : ""}
          <span class="li-count">${p.year || "?"}</span>
        </li>`
      ).join("") + `</ul>`;

  html += `<button class="detail-profile-btn">${t("author.more")}</button>`;

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
    };
  });
  const profileBtn = detail.querySelector(".detail-profile-btn");
  if (profileBtn) profileBtn.onclick = () => { setTab(4); spShowAuthorProfile(key); };
}

// ---------- department card ----------

function showDeptCard(did) {
  const d = deptById.get(did); if (!d) return;
  const partners = (DATA.dept_edges || [])
    .filter(e => e.s === did || e.t === did)
    .sort((a, b) => b.w - a.w);
  let html = `<div class="card-kind">${t("dept.kind")}</div><div class="card-title">${esc(deptDisplayName(d))}</div>`;
  html += `<div class="card-row"><b>${t("dept.authorsCount")}</b> ${d.n_authors || 0}</div>`;
  html += `<div class="card-row"><b>${t("dept.pubsCount")}</b> ${d.n_pubs || 0}</div>`;
  if (d.n_repos) html += `<div class="card-row"><b>${t("dept.reposCount")}</b> ${d.n_repos}</div>`;
  html += `<div class="card-section">${t("dept.collaboratesWith", partners.length)}</div><ul class="card-list">`;
  partners.slice(0, 15).forEach(e => {
    const oid = e.s === did ? e.t : e.s, o = deptById.get(oid); if (!o) return;
    html += `<li data-d="${oid}"><div class="li-name" style="display:flex;align-items:center;gap:10px"><span class="dept-dot" style="background:${o.color}"></span>${esc(deptDisplayName(o))}</div><span class="li-count">${e.w}</span></li>`;
  });
  html += `</ul>`;
  html += `<button class="detail-profile-btn">${t("dept.more")}</button>`;
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