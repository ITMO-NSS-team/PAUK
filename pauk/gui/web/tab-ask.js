"use strict";

// Tab 6: a free-text question to the graph (pauk/search via POST /api/ask).
// Everything here is built from DOM nodes, never innerHTML with server text:
// titles, names and the LLM's answer all arrive from the server.

const ASK_EXAMPLES = ["ask.ex1", "ask.ex2", "ask.ex3", "ask.ex4"];
const ASK_SECTIONS = ["persons", "departments", "publications", "repositories"];
let _askBusy = false;
let _askBuilt = false;

function askEl(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function askExternal(text, url) {
  return /^https?:\/\//i.test(url || "")
    ? askEl("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, text)
    : askEl("span", {}, text);
}

// A result that is also a node on the map opens its profile on the Search tab.
function askMapLink(kind, key) {
  const node = nodeByKey.get(key);
  if (!node || node.kind !== kind) return null;
  const open = { author: spShowAuthorProfile, pub: spShowPubProfile, repo: spShowRepoProfile }[kind];
  return askEl("button", {
    class: "ask-maplink", type: "button", title: t("ask.onMapTitle"),
    onclick: () => { setTab(4); open(key); },
  }, t("ask.onMap"));
}

function askDeptLink(name) {
  const dept = DATA.departments.find(d => d.name === name || d.name_en === name);
  if (!dept) return null;
  return askEl("button", {
    class: "ask-maplink", type: "button", title: t("ask.onMapTitle"),
    onclick: () => { setTab(4); spShowDeptProfile(dept.id); },
  }, t("ask.onMap"));
}

// LLM №2 answers in Markdown; only links, bold and list items survive.
function askInline(text) {
  const out = [];
  const re = /\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)|\*\*([^*]+)\*\*/g;
  let last = 0;
  for (const m of text.matchAll(re)) {
    if (m.index > last) out.push(text.slice(last, m.index));
    out.push(m[1] ? askExternal(m[1], m[2]) : askEl("b", {}, m[3]));
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function askAnswer(markdown) {
  const box = askEl("div", { class: "ask-answer" });
  let list = null;
  for (const raw of markdown.split("\n")) {
    const line = raw.trim();
    if (!line) { list = null; continue; }
    const item = line.match(/^(?:[-*•]|\d+[.)])\s+(.*)$/);
    if (item) {
      if (!list) { list = askEl("ul"); box.append(list); }
      list.append(askEl("li", {}, askInline(item[1])));
    } else {
      list = null;
      box.append(askEl("p", {}, askInline(line.replace(/^#+\s*/, ""))));
    }
  }
  return box;
}

const askScore = v => (typeof v === "number" ? v.toFixed(3) : "");

function askTable(headers, rows) {
  return askEl("div", { class: "ask-table-wrap" },
    askEl("table", { class: "ask-table" },
      askEl("thead", {}, askEl("tr", {}, headers.map(([title, cls]) => askEl("th", { class: cls }, title)))),
      askEl("tbody", {}, rows)));
}

const ASK_RENDER = {
  persons: items => askTable(
    [[t("ask.col.name")], [t("ask.col.units")], [t("ask.col.pubs"), "num"], [t("ask.col.examples")],
     [t("ask.col.code")], [t("ask.col.score"), "num"]],
    items.map(p => askEl("tr", {},
      askEl("td", {}, askExternal(p.label, p.url), " ", askMapLink("author", p.id),
        p.name_en && p.name_en !== p.label ? askEl("div", { class: "ask-sub" }, p.name_en) : null),
      askEl("td", { class: "ask-sub" }, (p.departments || []).join("; ")),
      askEl("td", { class: "num" }, p.publications ?? ""),
      askEl("td", { class: "ask-sub" }, (p.top_publications || []).slice(0, 2).join(" · ")),
      askEl("td", { class: "ask-sub" }, (p.repositories || []).flatMap((url, i) =>
        [i ? ", " : null, askExternal(url.replace("https://github.com/", ""), url)])),
      askEl("td", { class: "num" }, askScore(p.score))))),
  departments: items => askTable(
    [[t("ask.col.unit")], [t("ask.col.kind")], [t("ask.col.pubs"), "num"], [t("ask.col.people"), "num"],
     [t("ask.col.score"), "num"]],
    items.map(d => askEl("tr", {},
      askEl("td", {}, d.label, " ", askDeptLink(d.label)),
      askEl("td", { class: "ask-sub" }, d.kind ? t("ask.kind." + d.kind) : ""),
      askEl("td", { class: "num" }, d.publications ?? ""),
      askEl("td", { class: "num" }, d.persons ?? ""),
      askEl("td", { class: "num" }, askScore(d.score))))),
  publications: items => askTable(
    [[t("ask.col.title")], [t("ask.col.year"), "num"], [t("ask.col.authors")], [t("ask.col.score"), "num"]],
    items.map(p => askEl("tr", {},
      askEl("td", {}, askExternal(p.label, p.url), " ", askMapLink("pub", p.id),
        p.journal ? askEl("div", { class: "ask-sub" }, p.journal) : null),
      askEl("td", { class: "num" }, p.year ?? ""),
      askEl("td", { class: "ask-sub" }, (p.authors || []).join(", ")),
      askEl("td", { class: "num" }, askScore(p.score))))),
  repositories: items => askTable(
    [[t("ask.col.repo")], [t("ask.col.description")], ["★", "num"], [t("ask.col.contributors")],
     [t("ask.col.score"), "num"]],
    items.map(r => askEl("tr", {},
      askEl("td", {}, askExternal(r.label, r.url), " ", askMapLink("repo", r.id)),
      askEl("td", { class: "ask-sub" }, r.description || ""),
      askEl("td", { class: "num" }, r.stars ?? ""),
      askEl("td", { class: "ask-sub" }, (r.contributors || []).join(", ")),
      askEl("td", { class: "num" }, askScore(r.score))))),
};

function askCounts(counts) {
  const stats = ["publications", "publications_total", "repositories", "coauthors", "members", "units"]
    .filter(k => counts[k] !== undefined);
  const years = Object.entries(counts.by_year || {});
  return askEl("div", { class: "ask-block" },
    askEl("div", { class: "ask-h" }, counts.entity ? t("ask.summaryOf", counts.entity) : t("ask.summary")),
    askEl("div", { class: "ask-tiles" }, stats.map(k =>
      askEl("div", { class: "st-tile" }, askEl("div", { class: "st-tile-n" }, counts[k]),
        askEl("div", { class: "st-tile-k" }, t("ask.count." + k))))),
    years.length ? askEl("div", { class: "ask-sub" },
      t("ask.byYear") + " " + years.map(([y, n]) => `${y} — ${n}`).join(", ")) : null);
}

function askTrace(result) {
  const parts = [askEl("div", { class: "ask-h" }, t("ask.trace.plan")),
    askEl("pre", { class: "ask-pre" }, JSON.stringify(result.plan, null, 2))];
  for (const e of result.entities) {
    parts.push(askEl("div", { class: "ask-h" }, t("ask.trace.entity", e.query) + (e.ambiguous ? " — " + t("ask.ambiguous") : "")));
    parts.push(askEl("pre", { class: "ask-pre" }, e.candidates.map(c =>
      `${c.score}\t${c.is_itmo ? "ITMO" : "ext"}\t${c.label}\t${c.id}`).join("\n") || t("ask.noMatch")));
  }
  if (result.entry_points.length) {
    parts.push(askEl("div", { class: "ask-h" }, t("ask.trace.entry")));
    parts.push(askTable([[t("ask.col.title")], [t("ask.col.score"), "num"], ["cos", "num"], ["BM25", "num"]],
      result.entry_points.map(h => askEl("tr", {},
        askEl("td", { class: "ask-sub" }, h.title || h.id), askEl("td", { class: "num" }, askScore(h.score)),
        askEl("td", { class: "num" }, h.dense ?? ""), askEl("td", { class: "num" }, h.bm25 ?? "")))));
  }
  parts.push(askEl("div", { class: "ask-h" }, t("ask.trace.timings")),
    askEl("pre", { class: "ask-pre" }, JSON.stringify(result.timings, null, 2)));
  if (result.trace && result.trace.parse) {
    parts.push(askEl("div", { class: "ask-h" }, t("ask.trace.raw")),
      askEl("pre", { class: "ask-pre" }, result.trace.parse.raw || result.trace.parse.fallback || ""));
  }
  return askEl("details", { class: "ask-trace" }, askEl("summary", {}, t("ask.trace")), parts);
}

function askRenderResult(result) {
  const out = document.getElementById("ask-result");
  out.replaceChildren();
  for (const note of result.notes) out.append(askEl("div", { class: "st-err" }, note));
  if (result.answer) out.append(askAnswer(result.answer));
  if (result.sections.counts) out.append(askCounts(result.sections.counts));
  for (const name of ASK_SECTIONS) {
    const items = result.sections[name];
    if (!items) continue;
    out.append(askEl("div", { class: "ask-block" },
      askEl("div", { class: "ask-h" }, `${t("ask.section." + name)} (${items.length})`),
      items.length ? ASK_RENDER[name](items) : askEl("div", { class: "ask-sub" }, t("ask.nothing"))));
  }
  out.append(askTrace(result));
}

async function askSubmit(event) {
  event.preventDefault();
  const question = document.getElementById("ask-input").value.trim();
  if (!question || _askBusy) return;
  _askBusy = true;
  const button = document.getElementById("ask-submit");
  const status = document.getElementById("ask-status");
  button.disabled = true;
  status.textContent = t("ask.loading");
  try {
    const res = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question,
        retrieval_mode: document.getElementById("ask-mode").value,
        ranking: document.getElementById("ask-ranking").value,
        llm_parse: document.getElementById("ask-llm-parse").checked,
        llm_answer: document.getElementById("ask-llm-answer").checked,
        top_n: Number(document.getElementById("ask-top").value) || 10,
      }),
    });
    const payload = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error([payload.error || `HTTP ${res.status}`, payload.detail].filter(Boolean).join(" — "));
    status.textContent = "";
    askRenderResult(payload);
  } catch (err) {
    status.textContent = "";
    document.getElementById("ask-result").replaceChildren(askEl("div", { class: "st-err" }, err.message));
  } finally {
    _askBusy = false;
    button.disabled = false;
  }
}

function renderAsk() {
  if (_askBuilt) return;
  _askBuilt = true;
  const option = (value, key) => askEl("option", { value }, t(key));
  const page = document.getElementById("ask-page");
  page.replaceChildren(askEl("div", { class: "st-wrap" },
    askEl("div", { class: "st-title" }, t("ask.title")),
    askEl("div", { class: "st-meta" }, t("ask.subtitle")),
    askEl("form", { id: "ask-form", class: "ask-form", onsubmit: askSubmit },
      askEl("textarea", { id: "ask-input", rows: 2, placeholder: t("ask.placeholder"), required: true,
        onkeydown: e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); askSubmit(e); } } }),
      askEl("div", { class: "ask-examples" }, ASK_EXAMPLES.map(key => askEl("button", {
        type: "button", class: "ask-chip",
        onclick: () => { const input = document.getElementById("ask-input"); input.value = t(key); input.focus(); },
      }, t(key)))),
      askEl("div", { class: "ask-options" },
        askEl("label", {}, t("ask.opt.mode"), askEl("select", { id: "ask-mode" },
          option("hybrid", "ask.mode.hybrid"), option("dense", "ask.mode.dense"), option("bm25", "ask.mode.bm25"))),
        askEl("label", {}, t("ask.opt.ranking"), askEl("select", { id: "ask-ranking" },
          option("graph", "ask.ranking.graph"), option("authorship", "ask.ranking.authorship"))),
        askEl("label", {}, t("ask.opt.top"), askEl("input", { id: "ask-top", type: "number", min: 1, max: 100, value: 10 })),
        askEl("label", {}, askEl("input", { id: "ask-llm-parse", type: "checkbox", checked: true }), t("ask.opt.llmParse")),
        askEl("label", {}, askEl("input", { id: "ask-llm-answer", type: "checkbox", checked: true }), t("ask.opt.llmAnswer")),
        askEl("button", { id: "ask-submit", class: "st-btn", type: "submit" }, t("ask.submit")))),
    askEl("div", { id: "ask-status", class: "ask-sub" }),
    askEl("div", { id: "ask-result" })));
}
