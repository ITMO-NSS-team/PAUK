"use strict";

function squareImg(color, size) {
  const cv = document.createElement("canvas"); cv.width = cv.height = size;
  const g = cv.getContext("2d");
  g.fillStyle = color; g.fillRect(1, 1, size - 2, size - 2);
  g.strokeStyle = "rgba(0,0,0,0.6)"; g.lineWidth = 1; g.strokeRect(0.5, 0.5, size - 1, size - 1);
  return g.getImageData(0, 0, size, size);
}

function ensureSquareImage(color) {
  const id = "sq" + color.replace("#", "");
  if (!map.hasImage(id)) map.addImage(id, squareImg(color, 16), { pixelRatio: 2 });
  return id;
}

function showPubCard(key) {
  const n = nodeByKey.get(key); if (!n) return;
  const au = (pubAuthors.get(key) || []).map(k => nodeByKey.get(k)).filter(Boolean);
  let html = `<div class="card-kind">публикация${n.year ? " · " + n.year : ""}</div><div class="card-title">${esc(n.label || n.key)}</div>`;
  if (n.journal) html += `<div class="card-row"><b>Журнал:</b> ${esc(n.journal)}</div>`;
  const deptName = deptById.get(n.dept)?.name; if (deptName) html += `<div class="card-row"><b>Департамент:</b> ${esc(deptName)}</div>`;
  if (n.doi) html += `<div class="card-row"><b>DOI:</b> <a href="https://doi.org/${esc(n.doi.replace(/^https?:\/\/doi\.org\//, ""))}" target="_blank">${esc(n.doi)}</a></div>`;
  const urls = Array.isArray(n.code_url) ? n.code_url : [];
  html += `<div class="card-row"><b>Код:</b> ${n.has_code && urls.length
    ? urls.map(u => `<a href="${esc(u)}" target="_blank">${esc(u.replace("https://github.com/", ""))}</a>`).join(", ")
    : '<span class="tag gray">нет</span>'}</div>`;
  html += `<div class="card-section">Авторы ИТМО (${au.length})</div><ul class="card-list">`;
  au.forEach(a => html += `<li data-k="${esc(a.key)}">${esc(a.label)}</li>`);
  html += `</ul>`;
  html += `<button class="detail-profile-btn">Подробнее о публикации →</button>`;
  showDetail(html);
  const pubProfileBtn = detailBody.querySelector(".detail-profile-btn");
  if (pubProfileBtn) pubProfileBtn.onclick = () => { setTab(4); spShowPubProfile(key); };
  detailBody.querySelectorAll("li[data-k]").forEach(li => {
    li.onclick = () => {
      const k = li.getAttribute("data-k");
      const nd = nodeByKey.get(k); if (!nd) return;
      const targetTab = nd.kind === "repo" ? 2 : nd.kind === "pub" ? 3 : 1;
      if (targetTab !== tab) setTab(targetTab);
      selectNode(k);
      map.flyTo({ center: proj(...P(k)), zoom: Math.max(map.getZoom(), 8), duration: 600 });
    };
  });
}
