"use strict";

// Developer page for `pauk search batch` runs: every question with what the
// system understood, what it found and what it answered, plus the marks
// (correct / broken_step / comment) that are saved back into review.csv.
// DOM is built from nodes, never innerHTML: all text comes from the data.

const STEPS = ["parse", "entities", "retrieval", "template", "ranking", "answer", "data", "unsupported"];
const MARKS = ["да", "частично", "нет"];
const MARK_CLASS = { "да": "ok", "частично": "part", "нет": "bad" };
const SECTION_TITLES = { persons: "Люди", departments: "Подразделения", publications: "Публикации",
  repositories: "Репозитории" };
const STAGE_COLORS = { parse: "#9D9D9D", entities: "#7F7F7F", retrieval: "#3555e0", graph: "#1f7a3f",
  ranking: "#d39b26", answer: "#616161" };

// mode "pool": grade the merged top-k of every search variant (pauk search pool)
const state = { run: null, results: [], index: 0, mode: "answers", pool: null, judgments: {}, progress: {} };
const GRADE_TITLES = { 0: "не по теме", 1: "частично", 2: "точно по теме" };
const $ = id => document.getElementById(id);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false || child === "") continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

const link = (text, url) => /^https?:\/\//i.test(url || "")
  ? el("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, text) : el("span", {}, text);
const score = v => (typeof v === "number" ? v.toFixed(3) : "");

async function api(path, options) {
  const res = await fetch(path, options);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
  return body;
}

// --- list and filters ------------------------------------------------------

function visible() {
  const type = $("f-type").value, mark = $("f-mark").value;
  return state.results.map((r, i) => [r, i]).filter(([r]) =>
    (!type || (r.meta && r.meta.type) === type) &&
    (!mark || (mark === "-" ? !r.marks.correct : r.marks.correct === mark)));
}

function renderList() {
  const nav = $("list");
  nav.replaceChildren(...visible().map(([r, i]) => el("button", {
    class: "q" + (i === state.index ? " active" : ""), type: "button", onclick: () => select(i),
  },
    el("span", { class: "n" }, i + 1),
    el("span", { class: "text" }, r.question),
    state.mode === "pool" ? poolListMeta(r) : el("span", { class: "meta" },
      el("span", { class: "dot " + (MARK_CLASS[r.marks.correct] || "") }),
      [r.meta && r.meta.type, r.marks.broken_step, r.error && "ошибка"].filter(Boolean).join(" · ")))));
  const active = nav.querySelector(".active");
  if (active) active.scrollIntoView({ block: "nearest" });
}

function renderSummary() {
  if (state.mode === "pool") {
    const all = Object.values(state.progress);
    const judged = all.reduce((a, p) => a + p.judged, 0), total = all.reduce((a, p) => a + p.total, 0);
    $("summary").replaceChildren(
      el("span", { class: "pill" + (total && judged === total ? " ok" : "") }, `оценено ${judged} из ${total}`),
      el("span", { class: "pill" }, "метрики: uv run pauk search metrics"));
    return;
  }
  const count = v => state.results.filter(r => r.marks.correct === v).length;
  const steps = {};
  for (const r of state.results) if (r.marks.broken_step) steps[r.marks.broken_step] = (steps[r.marks.broken_step] || 0) + 1;
  const unmarked = state.results.filter(r => !r.marks.correct).length;
  $("summary").replaceChildren(
    el("span", { class: "pill ok" }, `да ${count("да")}`),
    el("span", { class: "pill part" }, `частично ${count("частично")}`),
    el("span", { class: "pill bad" }, `нет ${count("нет")}`),
    el("span", { class: "pill" }, `не размечено ${unmarked}`),
    ...Object.entries(steps).sort((a, b) => b[1] - a[1]).map(([s, n]) => el("span", { class: "pill" }, `${s}: ${n}`)));
}

// --- one question ------------------------------------------------------------

function markdown(text) {
  const box = el("div", { class: "answer" });
  let list = null;
  const inline = line => {
    const out = [];
    const re = /\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)|\*\*([^*]+)\*\*/g;
    let last = 0;
    for (const m of line.matchAll(re)) {
      if (m.index > last) out.push(line.slice(last, m.index));
      out.push(m[1] ? link(m[1], m[2]) : el("b", {}, m[3]));
      last = m.index + m[0].length;
    }
    if (last < line.length) out.push(line.slice(last));
    return out;
  };
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line) { list = null; continue; }
    const item = line.match(/^(?:[-*•]|\d+[.)])\s+(.*)$/);
    if (item) {
      if (!list) { list = el("ul"); box.append(list); }
      list.append(el("li", {}, inline(item[1])));
    } else {
      list = null;
      box.append(el("p", {}, inline(line.replace(/^#+\s*/, ""))));
    }
  }
  return box;
}

function table(headers, rows) {
  return el("div", { class: "table-wrap" }, el("table", {},
    el("thead", {}, el("tr", {}, headers.map(([h, cls]) => el("th", { class: cls }, h)))),
    el("tbody", {}, rows)));
}

const SECTION_RENDER = {
  persons: items => table([["Имя"], ["Подразделения"], ["Публ.", "num"], ["Примеры работ"], ["Код"], ["Балл", "num"]],
    items.map(p => el("tr", {},
      el("td", {}, link(p.label, p.url), p.is_itmo === false ? el("div", { class: "small" }, "не ИТМО") : null),
      el("td", { class: "small" }, (p.departments || []).join("; ")),
      el("td", { class: "num" }, p.publications ?? ""),
      el("td", { class: "small" }, (p.top_publications || []).join(" · ")),
      el("td", { class: "small" }, (p.repositories || []).flatMap((u, i) => [i ? ", " : null,
        link(u.replace("https://github.com/", ""), u)])),
      el("td", { class: "num" }, score(p.score))))),
  departments: items => table([["Подразделение"], ["Тип"], ["Публ.", "num"], ["Людей", "num"], ["Балл", "num"]],
    items.map(d => el("tr", {}, el("td", {}, d.label), el("td", { class: "small" }, d.kind || ""),
      el("td", { class: "num" }, d.publications ?? ""), el("td", { class: "num" }, d.persons ?? ""),
      el("td", { class: "num" }, score(d.score))))),
  publications: items => table([["Название"], ["Год", "num"], ["Авторы"], ["Балл", "num"]],
    items.map(p => el("tr", {},
      el("td", {}, link(p.label, p.url), p.journal ? el("div", { class: "small" }, p.journal) : null),
      el("td", { class: "num" }, p.year ?? ""), el("td", { class: "small" }, (p.authors || []).join(", ")),
      el("td", { class: "num" }, score(p.score))))),
  repositories: items => table([["Репозиторий"], ["Описание"], ["★", "num"], ["Контрибьюторы"], ["Балл", "num"]],
    items.map(r => el("tr", {}, el("td", {}, link(r.label, r.url)), el("td", { class: "small" }, r.description || ""),
      el("td", { class: "num" }, r.stars ?? ""), el("td", { class: "small" }, (r.contributors || []).join(", ")),
      el("td", { class: "num" }, score(r.score))))),
};

function countsBlock(counts) {
  const keys = { publications: "публикаций (с фильтром)", publications_total: "публикаций всего",
    repositories: "репозиториев", coauthors: "соавторов", members: "сотрудников", units: "подразделений" };
  const years = Object.entries(counts.by_year || {});
  return el("section", {}, el("h2", {}, "Сводка" + (counts.entity ? `: ${counts.entity}` : "")),
    el("div", { class: "tiles" }, Object.entries(keys).filter(([k]) => counts[k] !== undefined)
      .map(([k, label]) => el("div", { class: "tile" }, el("b", {}, counts[k]), el("span", {}, label)))),
    years.length ? el("div", { class: "small", style: "margin-top:6px" },
      "По годам: " + years.map(([y, n]) => `${y} — ${n}`).join(", ")) : null);
}

function planBlock(r) {
  const plan = r.plan || {};
  const filter = plan.filter || {};
  const chips = list => (list || []).length ? list.map(x => el("span", { class: "chip" }, x)) : el("span", { class: "small" }, "—");
  const parse = (r.trace && r.trace.parse) || {};
  return el("section", {}, el("h2", {}, "Как понят вопрос (LLM №1)"),
    el("dl", { class: "kv" },
      el("dt", {}, "тип"), el("dd", {}, `${plan.type || "—"} · ${plan.graph_type || "—"}`),
      el("dt", {}, "тема (core)"), el("dd", {}, chips(plan.core)),
      el("dt", {}, "сущности"), el("dd", {}, chips((plan.entities || []).map(e => `${e.kind}: ${e.name}`))),
      el("dt", {}, "разделы"), el("dd", {}, chips(plan.expected_values)),
      el("dt", {}, "поля"), el("dd", {}, chips(plan.fields)),
      el("dt", {}, "фильтр"), el("dd", {}, `ИТМО: ${filter.only_itmo ? "да" : "нет"}`
        + (filter.year_from ? `, с ${filter.year_from}` : "") + (filter.year_to ? `, по ${filter.year_to}` : "")),
      parse.fallback ? el("dt", {}, "сбой разбора") : null, parse.fallback ? el("dd", {}, parse.fallback) : null),
    el("details", { style: "margin-top:8px" }, el("summary", {}, "Сырой ответ LLM №1 и план целиком"),
      el("pre", {}, parse.raw || "—"), el("pre", {}, JSON.stringify(plan, null, 2))));
}

function entitiesBlock(r) {
  if (!(r.entities || []).length) return null;
  return el("section", {}, el("h2", {}, "Поиск сущностей по имени"),
    ...r.entities.map(e => el("div", { style: "margin-bottom:10px" },
      el("div", { class: "small" }, `«${e.query}» (${e.kind})` + (e.ambiguous ? " — неоднозначно" : "")),
      e.candidates.length ? table([["Кандидат"], ["ИТМО"], ["Сходство", "num"], ["id"]],
        e.candidates.map((c, i) => el("tr", {}, el("td", {}, i === 0 ? el("b", {}, c.label) : c.label),
          el("td", {}, c.is_itmo ? "да" : ""), el("td", { class: "num" }, c.score),
          el("td", { class: "small" }, c.id)))) : el("div", { class: "small" }, "совпадений нет"))));
}

function entryBlock(r) {
  const hits = r.entry_points || [];
  const repoHits = (r.trace && r.trace.repository_hits) || [];
  if (!hits.length && !repoHits.length) return null;
  return el("section", {}, el("h2", {}, "Точки входа: найдено по смыслу"),
    hits.length ? table([["Публикация"], ["Итог", "num"], ["cos", "num"], ["BM25", "num"]],
      hits.map(h => el("tr", {}, el("td", { class: "small" }, h.title || h.id), el("td", { class: "num" }, score(h.score)),
        el("td", { class: "num" }, h.dense ?? ""), el("td", { class: "num" }, h.bm25 ?? "")))) : null,
    repoHits.length ? el("div", { class: "small", style: "margin-top:6px" },
      "Репозитории: " + repoHits.map(h => `${h.id} (${h.score})`).join(", ")) : null);
}

function timingsBlock(r) {
  const timings = r.timings || {};
  const total = Object.values(timings).reduce((a, b) => a + b, 0);
  if (!total) return null;
  return el("section", {}, el("h2", {}, `Время: ${total.toFixed(1)} с`),
    el("div", { class: "bars" }, Object.entries(timings).map(([k, v]) =>
      el("div", { title: `${k}: ${v} с`, style: `width:${(100 * v / total).toFixed(2)}%;background:${STAGE_COLORS[k] || "#C4C4C4"}` }))),
    el("div", { class: "small" }, Object.entries(timings).map(([k, v]) => `${k} ${v.toFixed(2)} с`).join(" · ")));
}

let saveTimer = null;

function marksBar(r) {
  const status = el("span", { class: "save-state" });
  const save = () => {
    clearTimeout(saveTimer);
    status.textContent = "сохраняю…";
    status.classList.remove("err");
    saveTimer = setTimeout(async () => {
      try {
        const body = await api("/api/review/mark", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ run: state.run, question: r.question, marks: r.marks }) });
        r.marks = body.marks;
        status.textContent = "сохранено";
        renderList();
        renderSummary();
      } catch (err) {
        status.textContent = err.message;
        status.classList.add("err");
      }
    }, 400);
  };
  const seg = el("div", { class: "seg" }, MARKS.map((v, i) => el("button", {
    type: "button", "data-v": v, class: r.marks.correct === v ? "on" : "", title: `клавиша ${i + 1}`,
    onclick: e => {
      r.marks.correct = r.marks.correct === v ? "" : v;
      for (const b of seg.children) b.classList.toggle("on", b.dataset.v === r.marks.correct);
      save();
    },
  }, v)));
  const step = el("select", { onchange: e => { r.marks.broken_step = e.target.value; save(); } },
    el("option", { value: "" }, "шаг поломки —"),
    STEPS.map(s => el("option", { value: s, selected: r.marks.broken_step === s }, s)));
  const comment = el("textarea", { placeholder: "комментарий: что не так и почему", rows: 5,
    oninput: e => { r.marks.comment = e.target.value; save(); } });
  comment.value = r.marks.comment || "";
  return el("div", { class: "marks" }, seg, step, comment, status);
}

// --- pool: grading the merged results of all variants ---------------------------

function poolEntry(question) {
  return state.pool && state.pool.questions.find(q => q.question === question);
}

function poolListMeta(r) {
  const entry = poolEntry(r.question);
  const p = state.progress[r.question];
  const text = !entry ? "нет в пуле" : entry.skipped ? "без темы — не оценивается" : `оценено ${p.judged}/${p.total}`;
  const cls = p && p.total && p.judged === p.total ? "ok" : p && p.judged ? "part" : "";
  return el("span", { class: "meta" }, el("span", { class: "dot " + cls }), text);
}

async function judge(question, kind, id, grade, row) {
  const key = `${kind}:${id}`;
  const grades = state.judgments[question] || (state.judgments[question] = {});
  const previous = grades[key];
  const next = previous === grade ? null : grade;  // clicking the set grade again clears it
  if (next === null) delete grades[key]; else grades[key] = next;
  paintGrades(row, next);
  try {
    await api("/api/review/judge", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run: state.run, question, kind, id, grade: next }) });
    const p = state.progress[question];
    if (p) p.judged += (next === null ? 0 : 1) - (previous === undefined ? 0 : 1);
    renderList();
    renderSummary();
  } catch (err) {
    if (previous === undefined) delete grades[key]; else grades[key] = previous;
    paintGrades(row, previous);
    alert(`Не сохранено: ${err.message}`);
  }
}

function paintGrades(row, grade) {
  row.classList.toggle("judged", grade !== undefined && grade !== null);
  for (const b of row.querySelectorAll(".grade button")) b.classList.toggle("on", Number(b.dataset.g) === grade);
}

function poolItem(question, kind, id, item) {
  const grade = (state.judgments[question] || {})[`${kind}:${id}`];
  const body = kind === "persons"
    ? [el("div", {}, link(item.label, item.url), item.is_itmo === false ? el("span", { class: "small" }, " · не ИТМО") : null),
       (item.departments || []).length ? el("div", { class: "small" }, item.departments.join("; ")) : null,
       (item.publications || []).length ? el("ul", { class: "pool-pubs" }, item.publications.map(t => el("li", {}, t))) : null]
    : [el("div", {}, link(item.label, item.url)),
       el("div", { class: "small" }, [item.year, item.journal].filter(Boolean).join(" · ")
         + ((item.authors || []).length ? ` · ${item.authors.join(", ")}` : "")),
       item.abstract ? el("div", { class: "pool-abstract" }, item.abstract) : null];
  const row = el("div", { class: "pool-item" },
    el("div", { class: "grade" }, [0, 1, 2].map(g => el("button", {
      type: "button", "data-g": g, title: GRADE_TITLES[g], onclick: () => judge(question, kind, id, g, row),
    }, g))),
    el("div", { class: "pool-body" }, body));
  paintGrades(row, grade);
  return row;
}

function renderPoolDetail(r) {
  const entry = poolEntry(r.question);
  const head = [
    el("div", { class: "sub" }, `Вопрос ${state.index + 1} из ${state.results.length} · оценка пула`),
    el("h1", { class: "title" }, r.question),
    r.meta && r.meta.expected ? el("div", { class: "expected" }, el("b", {}, "Ожидается: "), r.meta.expected) : null,
    el("div", { class: "pool-hint" },
      "Оцените, насколько каждый результат отвечает на вопрос: ", el("b", {}, "0"), " — не по теме, ",
      el("b", {}, "1"), " — частично, ", el("b", {}, "2"), " — точно по теме. Повторный клик снимает оценку. ",
      "Порядок перемешан, и какой вариант поиска что нашёл, не показано, чтобы не подыгрывать."),
  ];
  let rest;
  if (!state.pool) rest = [el("div", { class: "empty" }, "Пула нет. Соберите: uv run pauk search pool --run " + state.run)];
  else if (!entry) rest = [el("div", { class: "empty" }, "Этого вопроса нет в пуле: пересоберите пул.")];
  else if (entry.skipped) rest = [el("div", { class: "empty" }, `Не оценивается: ${entry.skipped}.`)];
  else rest = state.pool.kinds.map(kind => el("section", {},
    el("h2", {}, `${SECTION_TITLES[kind]} (${entry.order[kind].length})`),
    entry.order[kind].map(id => poolItem(r.question, kind, id, entry.items[kind][id]))));
  $("detail").replaceChildren(...[...head, ...rest].filter(Boolean));
  $("detail").scrollTop = 0;
}

function renderDetail() {
  const main = $("detail");
  if (state.mode === "pool" && state.results[state.index]) { renderPoolDetail(state.results[state.index]); return; }
  const r = state.results[state.index];
  if (!r) { main.replaceChildren(el("div", { class: "empty" }, "Нет вопросов")); return; }
  const sections = r.sections || {};
  const meta = r.meta || {};
  // replaceChildren, unlike el(), would print a null as the text "null".
  main.replaceChildren(...[
    el("div", { class: "sub" }, `Вопрос ${state.index + 1} из ${state.results.length}` + (meta.type ? ` · ${meta.type}` : ""),
      el("span", { class: "keys" }, "  ↑/↓ — вопросы, 1/2/3 — оценка")),
    el("h1", { class: "title" }, r.question),
    meta.expected ? el("div", { class: "expected" }, el("b", {}, "Ожидается: "), meta.expected) : null,
    marksBar(r),
    r.error ? el("section", { class: "notes" }, el("div", { class: "err" }, r.error)) : null,
    (r.notes || []).length ? el("section", { class: "notes" }, el("h2", {}, "Заметки системы"),
      r.notes.map(n => el("div", {}, n))) : null,
    r.answer ? el("section", {}, el("h2", {}, "Ответ (LLM №2)"), markdown(r.answer)) : null,
    sections.counts ? countsBlock(sections.counts) : null,
    ...Object.keys(SECTION_TITLES).filter(k => sections[k]).map(k => el("section", {},
      el("h2", {}, `${SECTION_TITLES[k]} (${sections[k].length})`),
      sections[k].length ? SECTION_RENDER[k](sections[k]) : el("div", { class: "small" }, "пусто"))),
    r.plan ? planBlock(r) : null,
    entitiesBlock(r),
    entryBlock(r),
    timingsBlock(r),
  ].filter(Boolean));
  main.scrollTop = 0;
}

function select(i) {
  state.index = i;
  const url = new URL(location.href);
  url.searchParams.set("q", String(i + 1));
  history.replaceState(null, "", url);
  renderList();
  renderDetail();
}

// --- loading -------------------------------------------------------------------

async function loadRun(name) {
  state.run = name;
  const body = await api(`/api/review/run?name=${encodeURIComponent(name)}`);
  state.results = body.results;
  try {
    const pool = await api(`/api/review/pool?name=${encodeURIComponent(name)}`);
    Object.assign(state, { pool: pool.pool, judgments: pool.judgments, progress: pool.progress });
  } catch {
    Object.assign(state, { pool: null, judgments: {}, progress: {} });
  }
  const types = [...new Set(state.results.map(r => r.meta && r.meta.type).filter(Boolean))];
  $("f-type").replaceChildren(el("option", { value: "" }, "все"), types.map(t => el("option", { value: t }, t)));
  const fromUrl = Number(new URL(location.href).searchParams.get("q")) - 1;
  state.index = fromUrl >= 0 && fromUrl < state.results.length ? fromUrl : 0;
  renderSummary();
  renderList();
  renderDetail();
}

async function init() {
  try {
    const { runs } = await api("/api/review/runs");
    if (!runs.length) {
      $("detail").replaceChildren(el("div", { class: "empty" },
        "Прогонов нет. Запустите: uv run pauk search batch data/search/questions.csv"));
      return;
    }
    const wanted = new URL(location.href).searchParams.get("run");
    state.mode = new URL(location.href).searchParams.get("mode") === "pool" ? "pool" : "answers";
    $("mode").value = state.mode;
    $("mode").addEventListener("change", e => {
      state.mode = e.target.value;
      const url = new URL(location.href);
      url.searchParams.set("mode", state.mode);
      history.replaceState(null, "", url);
      renderSummary();
      renderList();
      renderDetail();
    });
    $("run").replaceChildren(...runs.map(r => el("option", { value: r.name, selected: r.name === wanted },
      `${r.name} (${r.questions}, ${new Date(r.modified * 1000).toLocaleString("ru")})`)));
    $("run").addEventListener("change", e => {
      const url = new URL(location.href);
      url.searchParams.set("run", e.target.value);
      url.searchParams.delete("q");
      history.replaceState(null, "", url);
      loadRun(e.target.value);
    });
    await loadRun($("run").value);
  } catch (err) {
    $("detail").replaceChildren(el("div", { class: "empty" }, err.message));
  }
}

for (const id of ["f-type", "f-mark"]) $(id).addEventListener("change", renderList);

document.addEventListener("keydown", e => {
  if (e.target.closest("textarea, select, input") || e.metaKey || e.ctrlKey || e.altKey) return;
  const shown = visible().map(([, i]) => i);
  const pos = shown.indexOf(state.index);
  if (e.key === "ArrowDown" || e.key === "j") {
    if (pos < shown.length - 1) select(shown[pos + 1]);
    e.preventDefault();
  } else if (e.key === "ArrowUp" || e.key === "k") {
    if (pos > 0) select(shown[pos - 1]);
    e.preventDefault();
  } else if (state.mode === "answers" && ["1", "2", "3"].includes(e.key)) {
    const button = document.querySelector(`.seg button[data-v="${MARKS[Number(e.key) - 1]}"]`);
    if (button) button.click();
  }
});

init();
