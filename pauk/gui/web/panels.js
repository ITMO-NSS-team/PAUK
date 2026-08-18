"use strict";

var detail     = document.getElementById("detail");
var detailBody = document.getElementById("detail-body");
var overview   = document.getElementById("overview");
document.getElementById("detail-close").onclick = () => clearAll();

function showDetail(html) {
  detailBody.innerHTML = html;
  detail.classList.remove("hidden");
  overview.classList.add("hidden");
  detail.scrollTop = 0;
}

function closePanel() {
  detail.classList.add("hidden");
  overview.classList.remove("hidden");
}

function renderOverview() {
  const fmt = n => n.toLocaleString(LANG === "en" ? "en-US" : "ru-RU");
  const tabKeys = { 1: "tab.people", 2: "tab.repos", 3: "tab.pubs" };
  // Same three tones as the rest of the chrome (coral/blue from
  // opensource.itmo.ru, amber our own pick) — not new colors, just reused here.
  const tabColors  = { 1: "var(--accent-2)", 2: "#d9962e", 3: "#e8562e" };
  const tabColor = tabColors[tab] || "inherit";
  overview.innerHTML =
    `<img class="overview-web" src="vendor/icons/pauk-web-4x.png" alt="" />` +
    `<div class="card-label">${t("overview.label")} · <span style="color:${tabColor}">${t(tabKeys[tab] || "tab.people")}</span></div>` +
    `<div class="stat-grid">` +
    `<div class="stat"><div class="stat-num">${fmt(DATA.authors.length)}</div><div class="stat-lbl">${t("overview.authors")}</div></div>` +
    `<div class="stat"><div class="stat-num">${fmt(DATA.pubs.length)}</div><div class="stat-lbl">${t("overview.pubs")}</div></div>` +
    `<div class="stat"><div class="stat-num">${fmt(DATA.repos.length)}</div><div class="stat-lbl">${t("overview.repos")}</div></div>` +
    `<div class="stat"><div class="stat-num">${fmt(DATA.departments.filter(d => d.name !== "Без департамента").length)}</div><div class="stat-lbl">${t("overview.depts")}</div></div>` +
    `</div>` +
    (tab === 2 ? `<div style="margin-top:16px;padding:11px 14px;background:var(--surface-2);border-radius:10px;font-size:14px;font-weight:700;color:var(--text);text-align:center;line-height:1.4">${t("overview.inDev")}</div>` : "");
}

function showNodeCard(key) {
  const n = nodeByKey.get(key); if (!n) return;
  if (n.kind === "author") { showAuthorProfile(key); return; }
  if (n.kind === "pub")    { showPubCard(key);       return; }
  if (n.kind === "repo")   { showRepoCard(key);      return; }
}

function showEdgeCard(props) {
  const sn = nodeByKey.get(props.s), tn = nodeByKey.get(props.t);
  if (!sn || !tn) return;
  const w = props.w || 1;
  let html = "";

  if (sn.kind === "author" && tn.kind === "author") {
    const sSet = new Set(authorPubs.get(props.s) || []);
    const shared = (authorPubs.get(props.t) || []).filter(p => sSet.has(p));
    html += `<div class="card-kind">${t("edge.coauthorship")}</div>`;
    html += `<div class="card-title">${esc(authorDisplayName(sn))} — ${esc(authorDisplayName(tn))}</div>`;
    html += `<div class="card-row"><b>${t("edge.jointPapersLabel")}</b> ${w}</div>`;
    if (shared.length) {
      html += `<div class="card-section">${t("edge.pubsSection", shared.length)}</div><ul class="card-list">`;
      shared.slice(0, 30).forEach(pk => {
        const p = nodeByKey.get(pk); if (!p) return;
        html += `<li data-k="${esc(pk)}"><div class="li-name">${esc(p.label || p.key)}</div><span class="li-count">${p.year || "?"}</span></li>`;
      });
      html += `</ul>`;
    }
  } else if (sn.kind === "pub" && tn.kind === "pub") {
    const sa = new Set(pubAuthors.get(props.s) || []);
    const shared = (pubAuthors.get(props.t) || []).filter(a => sa.has(a));
    html += `<div class="card-kind">${t("edge.pubPubKind")}</div>`;
    html += `<div class="card-row"><b>${esc(sn.label || sn.key)}</b></div>`;
    html += `<div class="card-row"><b>${esc(tn.label || tn.key)}</b></div>`;
    html += `<div class="card-row"><b>${t("edge.sharedAuthorsCountLabel")}</b> ${w}</div>`;
    if (shared.length) {
      html += `<div class="card-section">${t("edge.authorsSection")}</div><ul class="card-list">`;
      shared.forEach(ak => {
        const a = nodeByKey.get(ak); if (a) html += `<li data-k="${esc(ak)}">${esc(authorDisplayName(a))}</li>`;
      });
      html += `</ul>`;
    }
  } else if (sn.kind === "repo" && tn.kind === "repo") {
    html += `<div class="card-kind">${t("edge.repoRepoKind")}</div>`;
    html += `<div class="card-row"><a href="${esc(sn.url)}" target="_blank">${esc(sn.label)}</a></div>`;
    html += `<div class="card-row"><a href="${esc(tn.url)}" target="_blank">${esc(tn.label)}</a></div>`;
    html += `<div class="card-row"><b>${t("edge.sharedContributorsCountLabel")}</b> ${w}</div>`;
  } else {
    html += `<div class="card-kind">${t("edge.genericKind")}</div>`;
    html += `<div class="card-row"><b>${esc(authorDisplayName(sn))}</b></div>`;
    html += `<div class="card-row"><b>${esc(authorDisplayName(tn))}</b></div>`;
  }
  showDetail(html);
  detailBody.querySelectorAll("li[data-k]").forEach(li => {
    li.onclick = () => {
      const k = li.getAttribute("data-k");
      const nd = nodeByKey.get(k); if (!nd) return;
      const targetTab = nd.kind === "repo" ? 2 : nd.kind === "pub" ? 3 : 1;
      if (targetTab !== tab) setTab(targetTab);
      selectNode(k);
    };
  });
}

function _drawYearChart(svgEl, yearCounts) {
  if (!Object.keys(yearCounts).length) return;
  const allYears = [];
  const yrMin = Math.min(...Object.keys(yearCounts).map(Number));
  const yrMax = Math.max(...Object.keys(yearCounts).map(Number));
  for (let y = yrMin; y <= yrMax; y++) allYears.push(y);
  const maxVal = Math.max(...allYears.map(y => yearCounts[y] || 0));
  if (!maxVal) return;
  const W = svgEl.clientWidth || svgEl.parentElement?.clientWidth || 400;
  const H = +svgEl.getAttribute("height") || 120;
  const pad = { l: 12, r: 22, t: 20, b: 24 };
  const innerW = W - pad.l - pad.r;
  const single = allYears.length === 1;
  const inset  = single ? 0 : Math.min(40, innerW * 0.08);
  const xCenter = y => single ? pad.l + innerW / 2 : pad.l + inset + (y - yrMin) / Math.max(1, yrMax - yrMin) * (innerW - 2 * inset);
  const barW   = Math.min(32, Math.max(4, (innerW - 2 * inset) / Math.max(1, allYears.length) - 4));
  const yScale = v => H - pad.b - (v / maxVal) * (H - pad.t - pad.b);
  // fill/stroke reference the CSS variables directly (not resolved to a hex
  // string here) so the chart re-colors live on theme toggle instead of
  // keeping whatever color was baked in at draw time — a chart drawn in
  // light mode used to go dark-on-dark and vanish after switching to dark.
  const bar = "var(--text)", muted = "var(--muted)";
  let s = `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}">`;
  s += `<line x1="${pad.l}" y1="${pad.t}" x2="${W - pad.r}" y2="${pad.t}" stroke="${muted}" stroke-opacity="0.2" stroke-width="1"/>`;
  allYears.forEach(y => {
    const v = yearCounts[y] || 0, cx = xCenter(y), bx = cx - barW / 2, by = yScale(v), bh = H - pad.b - by;
    if (bh > 0) {
      s += `<rect x="${bx.toFixed(1)}" y="${by.toFixed(1)}" width="${barW.toFixed(1)}" height="${bh.toFixed(1)}" fill="${bar}" opacity="0.82" rx="1"/>`;
      s += `<text x="${cx.toFixed(1)}" y="${(by - 3).toFixed(1)}" font-size="9" fill="${muted}" text-anchor="middle">${v}</text>`;
    }
    if (allYears.length <= 12 || y % 2 === 0)
      s += `<text x="${cx.toFixed(1)}" y="${H - 4}" font-size="9" fill="${muted}" text-anchor="middle">${y}</text>`;
  });
  s += `</svg>`;
  svgEl.outerHTML = s;
}