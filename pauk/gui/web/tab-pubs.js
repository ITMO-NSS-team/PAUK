"use strict";

function squareImg(color, size) {
  const cv = document.createElement("canvas"); cv.width = cv.height = size;
  const g = cv.getContext("2d");
  const inset = size / 16; // was a fixed 1px at size=16; keep it proportional now that size doubled
  const r = size * 0.28;
  const [h, s, l] = hexToHsl(color);
  const grad = g.createLinearGradient(0, inset, 0, size - inset);
  grad.addColorStop(0, hslToHex(h, s, Math.min(1, l + 0.22)));
  grad.addColorStop(1, color);
  g.beginPath();
  g.roundRect(inset, inset, size - inset * 2, size - inset * 2, r);
  g.fillStyle = grad; g.fill();
  g.strokeStyle = "rgba(0,0,0,0.35)"; g.lineWidth = inset; g.stroke();
  return g.getImageData(0, 0, size, size);
}

function ensureSquareImage(color) {
  const id = "sq" + color.replace("#", "");
  if (!map.hasImage(id)) map.addImage(id, squareImg(color, 32), { pixelRatio: 4 });
  return id;
}

function showPubCard(key) {
  const n = nodeByKey.get(key); if (!n) return;
  const au = (pubAuthors.get(key) || []).map(k => nodeByKey.get(k)).filter(Boolean);
  const repos = (pubRepos.get(key) || []).map(k => nodeByKey.get(k)).filter(Boolean);
  let html = `<div class="card-kind">${t("pub.kind")}${n.year ? " · " + n.year : ""}</div><div class="card-title">${esc(n.label || n.key)}</div>`;
  if (n.journal) html += `<div class="card-row"><b>${t("pub.journal")}</b> ${esc(n.journal)}</div>`;
  const deptName = deptDisplayName(deptById.get(n.dept)); if (deptName) html += `<div class="card-row"><b>${t("pub.department")}</b> ${esc(deptName)}</div>`;
  if (n.doi) html += `<div class="card-row"><b>${t("pub.doi")}</b> <a href="https://doi.org/${esc(n.doi.replace(/^https?:\/\/doi\.org\//, ""))}" target="_blank">${esc(n.doi)}</a></div>`;
  if (repos.length) {
    html += `<div class="card-row"><b>${t("pub.repo")}</b> ${repos.map(r => `<a data-k="${esc(r.key)}" href="#">${esc(r.label)}</a>`).join(", ")}</div>`;
  } else {
    const urls = Array.isArray(n.code_url) ? n.code_url : [];
    html += `<div class="card-row"><b>${t("pub.code")}</b> ${n.has_code && urls.length
      ? urls.map(u => `<a href="${esc(u)}" target="_blank">${esc(u.replace("https://github.com/", ""))}</a>`).join(", ")
      : `<span class="tag gray">${t("pub.none")}</span>`}</div>`;
  }
  html += `<div class="card-section">${t("pub.itmoAuthors", au.length)}</div><ul class="card-list">`;
  au.forEach(a => html += `<li data-k="${esc(a.key)}">${esc(authorDisplayName(a))}</li>`);
  html += `</ul>`;
  html += `<button class="detail-profile-btn">${t("pub.more")}</button>`;
  showDetail(html);
  const pubProfileBtn = detailBody.querySelector(".detail-profile-btn");
  if (pubProfileBtn) pubProfileBtn.onclick = () => { setTab(4); spShowPubProfile(key); };
  detailBody.querySelectorAll("li[data-k], a[data-k]").forEach(li => {
    li.onclick = (e) => {
      e.preventDefault();
      const k = li.getAttribute("data-k");
      const nd = nodeByKey.get(k); if (!nd) return;
      const targetTab = nd.kind === "repo" ? 2 : nd.kind === "pub" ? 3 : 1;
      if (targetTab !== tab) setTab(targetTab);
      selectNode(k);
    };
  });
}
