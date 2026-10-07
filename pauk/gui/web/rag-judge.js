"use strict";

// Grading the RAG validation pool (pauk rag pool). Items are shown in the
// pool's shuffled order, so the judge cannot tell which variant ranked what.
// Built from DOM nodes only: names and abstracts come from the graph.

const KIND_TITLES = { persons: "Люди", publications: "Публикации", repositories: "Репозитории" };
const GRADE_TITLES = { 0: "не по теме", 1: "частично", 2: "точно по теме" };
const state = { run: null, judge: "", pool: null, judgments: {}, index: 0, focus: 0 };
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
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

const link = (text, url) => (url ? el("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, text) : text);
const entry = () => state.pool && state.pool.questions[state.index];
const grades = () => state.judgments[entry().question] || {};

function items() {
  const e = entry();
  return state.pool.kinds.flatMap(kind => e.order[kind].map(id => ({ kind, id, info: e.items[kind][id] })));
}

async function api(url, options) {
  const res = await fetch(url, options);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
  return body;
}

async function loadRuns() {
  const { runs } = await api("/api/rag/runs");
  $("run").replaceChildren(...runs.map(r => el("option", { value: r }, r)));
  const wanted = new URL(location.href).searchParams.get("run");
  if (wanted && runs.includes(wanted)) $("run").value = wanted;
  if (runs.length) await loadPool();
  else $("detail").replaceChildren(el("div", {}, "Нет прогонов: запустите pauk rag pool"));
}

async function loadPool() {
  state.run = $("run").value;
  state.judge = $("judge").value.trim();
  const query = new URLSearchParams({ run: state.run });
  if (state.judge) query.set("judge", state.judge);
  try {
    const body = await api(`/api/rag/pool?${query}`);
    state.pool = body.pool;
    state.judgments = body.judgments;
    state.index = Math.min(state.index, state.pool.questions.length - 1);
    state.focus = 0;
    render();
  } catch (err) {
    $("detail").replaceChildren(el("div", { class: "err" }, err.message));
  }
}

function questionProgress(e) {
  const g = state.judgments[e.question] || {};
  const keys = state.pool.kinds.flatMap(kind => Object.keys(e.items[kind]).map(id => `${kind}:${id}`));
  return { judged: keys.filter(k => k in g).length, total: keys.length };
}

function renderQuestions() {
  $("questions").replaceChildren(...state.pool.questions.map((e, i) => {
    const p = questionProgress(e);
    return el("div", { class: "q" + (i === state.index ? " active" : ""), onclick: () => select(i) },
      el("div", {}, e.question),
      el("div", { class: "meta" }, [e.meta.type, `${p.judged}/${p.total}`].filter(Boolean).join(" · ")),
      el("div", { class: "bar" }, el("span", { style: `width:${p.total ? (100 * p.judged) / p.total : 0}%` })));
  }));
}

function describe(kind, info) {
  if (kind === "persons") {
    return [
      el("div", {}, link(info.label, info.url), info.is_itmo === false ? el("span", { class: "small" }, " · не ИТМО") : null),
      (info.departments || []).length ? el("div", { class: "small" }, info.departments.join("; ")) : null,
      (info.evidence || []).length ? el("div", { class: "small" }, "Работы: " + info.evidence.join(" · ")) : null,
      (info.code || []).length ? el("div", { class: "small" }, "Код: " + info.code.join(" · ")) : null,
    ];
  }
  if (kind === "publications") {
    return [
      el("div", {}, link(info.title || "(без названия)", info.url)),
      el("div", { class: "small" }, [info.year, info.journal].filter(Boolean).join(" · ")),
      el("div", { class: "small" }, (info.authors || []).join(", ")),
      info.abstract ? el("div", {}, info.abstract) : null,
    ];
  }
  return [
    el("div", {}, link(info.title, info.url), info.stars ? el("span", { class: "small" }, ` · ★ ${info.stars}`) : null),
    info.description ? el("div", {}, info.description) : null,
    (info.implemented || []).length ? el("div", { class: "small" }, "Реализует: " + info.implemented.join(" · ")) : null,
    info.readme ? el("div", { class: "small" }, info.readme) : null,
  ];
}

function renderDetail() {
  const e = entry();
  const g = grades();
  const list = items();
  const blocks = [
    el("h2", {}, `Вопрос ${state.index + 1} из ${state.pool.questions.length}: ${e.question}`),
    e.meta.expected ? el("div", { class: "expected" }, el("b", {}, "Правило: "), e.meta.expected) : null,
  ];
  let position = 0;
  for (const kind of state.pool.kinds) {
    const ids = e.order[kind];
    blocks.push(el("h2", {}, `${KIND_TITLES[kind]} (${ids.length})`));
    if (!ids.length) blocks.push(el("div", { class: "small" }, "пусто"));
    for (const id of ids) {
      const key = `${kind}:${id}`;
      const mine = position++;
      const grade = g[key];
      blocks.push(el("div", {
        class: "item" + (grade !== undefined ? ` g${grade}` : "") + (mine === state.focus ? " focus" : ""),
        id: `item-${mine}`, onclick: () => { state.focus = mine; renderDetail(); },
      },
        el("div", { class: "grades" }, [0, 1, 2].map(v => el("button", {
          class: `v${v}` + (grade === v ? " on" : ""), title: GRADE_TITLES[v],
          onclick: ev => { ev.stopPropagation(); setGrade(kind, id, grade === v ? null : v); },
        }, String(v)))),
        el("div", {}, describe(kind, e.items[kind][id]))));
    }
  }
  $("detail").replaceChildren(...blocks.filter(Boolean));
  const focused = $(`item-${state.focus}`);
  if (focused && list.length) focused.scrollIntoView({ block: "nearest" });
}

function render() {
  renderQuestions();
  renderDetail();
}

function select(i) {
  state.index = i;
  state.focus = 0;
  $("detail").scrollTop = 0;
  render();
}

async function setGrade(kind, id, grade) {
  const question = entry().question;
  const key = `${kind}:${id}`;
  const before = grades()[key];
  const local = (state.judgments[question] = { ...grades() });
  if (grade === null) delete local[key];
  else local[key] = grade;
  render();
  try {
    await api("/api/rag/judge", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run: state.run, judge: state.judge || null, question, kind, id, grade }),
    });
    $("status").textContent = "сохранено";
  } catch (err) {
    // The server refused: show the grade it still has.
    if (before === undefined) delete local[key];
    else local[key] = before;
    $("status").textContent = "ошибка: " + err.message;
    render();
  }
}

document.addEventListener("keydown", ev => {
  if (!state.pool || ev.target.tagName === "INPUT") return;
  const list = items();
  if (ev.key === "ArrowDown") { state.focus = Math.min(state.focus + 1, list.length - 1); renderDetail(); }
  else if (ev.key === "ArrowUp") { state.focus = Math.max(state.focus - 1, 0); renderDetail(); }
  else if (ev.key === "]") select(Math.min(state.index + 1, state.pool.questions.length - 1));
  else if (ev.key === "[") select(Math.max(state.index - 1, 0));
  else if (["1", "2", "3", "0"].includes(ev.key) && list[state.focus]) {
    const { kind, id } = list[state.focus];
    setGrade(kind, id, ev.key === "0" ? null : Number(ev.key) - 1);
    state.focus = Math.min(state.focus + 1, list.length - 1);
  } else return;
  ev.preventDefault();
});

$("run").addEventListener("change", loadPool);
$("judge").addEventListener("change", loadPool);
loadRuns().catch(err => $("detail").replaceChildren(el("div", { class: "err" }, err.message)));
