"use strict";

function showRepoCard(key) {
  const n = nodeByKey.get(key); if (!n) return;
  const persons = (repoPersons.get(key) || [])
    .map(p => ({ ...p, node: nodeByKey.get(p.key) })).filter(p => p.node);
  let html = `<div class="card-kind">${t("repo.kind")}</div><div class="card-title">${esc(n.label)}</div>`;
  html += `<div class="card-row"><b>${t("repo.department")}</b> ${esc(deptDisplayName(deptById.get(n.dept)) || t("common.noDept"))}</div>`;
  if (n.stars)       html += `<div class="card-row"><b>${t("repo.stars")}</b> ★ ${n.stars}</div>`;
  if (n.description) html += `<div class="card-row">${esc(n.description)}</div>`;
  if (n.url)         html += `<div class="card-row"><a href="${esc(n.url)}" target="_blank">${esc(n.url.replace("https://github.com/", ""))}</a></div>`;
  if (persons.length) {
    html += `<div class="card-section">${t("repo.itmoMembers", persons.length)}</div><ul class="card-list">`;
    persons.forEach(p => html += `<li data-k="${esc(p.key)}">${esc(authorDisplayName(p.node))} <span class="tag gray">${esc(p.role)}</span></li>`);
    html += `</ul>`;
  }
  const rPubs = (repoPubs.get(key) || []).map(k => nodeByKey.get(k)).filter(Boolean)
    .sort((a, b) => (b.year || 0) - (a.year || 0));
  if (rPubs.length) {
    html += `<div class="card-section">${t("repo.pubsSection")}</div>`;
    html += `<ul class="card-list">` + rPubs.slice(0, 15).map(p =>
      `<li data-k="${esc(p.key)}">
        <div class="li-name">${esc(shortLabel(p.label || p.key))}</div>
        ${p.has_code ? `<span class="tag green" style="font-size:9px;padding:0 5px">${t("common.code")}</span>` : ""}
        <span class="li-count">${p.year || "?"}</span>
      </li>`
    ).join("") + `</ul>`;
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