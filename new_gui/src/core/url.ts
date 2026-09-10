// Слой "core" — чистая (без DOM/history) сериализация {screen, tab,
// selection} в параметры URL и обратно. Сознательно НЕ включает lang и
// filters — как и в старом GUI (main.js: _pushUrl/_replaceUrl носили только
// tab/kind/key/id), это состояние интерфейса самого пользователя, а не то,
// чем он делится ссылкой. Живую синхронизацию (history.pushState/popstate)
// делает features/urlSync.ts — здесь только преобразование данных в обе
// стороны, поэтому оно тестируется без единого DOM-события.

import type { GraphData } from "../contracts/graph";
import { indexByKey } from "./data";
import type { Screen, Selection, TabId } from "./state";

/**
 * Смысловые имена вкладок в query-строке вместо голых чисел ("tab=persons",
 * не "tab=1"). "persons" — по прямой просьбе, отдельно от внутреннего имени
 * `AuthorNode`/`authorsTab`/`"tab.authors"` — те не переименовывались, это
 * только видимая часть URL.
 */
const TAB_SLUGS: Record<TabId, string> = {
  1: "persons",
  2: "repos",
  3: "pubs",
};

/** Обратное сопоставление к {@link TAB_SLUGS} — слаг из URL обратно в {@link TabId}. */
const SLUG_TO_TAB: Record<string, TabId> = Object.fromEntries(
  Object.entries(TAB_SLUGS).map(([id, slug]) => [slug, Number(id) as TabId]),
);

/** Слаг меню — отдельно от {@link TAB_SLUGS}: меню не вкладка, у него нет `TabId`. */
const MENU_SLUG = "start";

/**
 * Сериализует текущие `screen`/`tab`/`selection` в строку параметров URL —
 * ровно то, что дальше передаётся в `history.pushState`/`replaceState` (см.
 * features/urlSync.ts). На меню (`screen === "menu"`) в строке нет ничего,
 * кроме `tab=start` — там нечего выбирать, `tab`/`selection` из состояния
 * при этом игнорируются. Вес ребра (`w`) сознательно не кладётся в
 * результат — при разборе ({@link parseUrlState}) он заново берётся из
 * `data`, а не из URL, чтобы ссылка не могла "соврать" о весе.
 *
 * @param state - минимальный срез состояния приложения, который стоит
 *   отражать в адресной строке.
 * @returns Строка вида `"tab=start"`, `"tab=persons"` или
 *   `"tab=persons&sel=node&key=A1"` — без ведущего `"?"` (его добавляет
 *   вызывающий код перед `pushState`/`replaceState`).
 *
 * @example
 * serializeUrlState({ screen: "menu", tab: 1, selection: null });
 * // "tab=start"
 *
 * serializeUrlState({ screen: "app", tab: 1, selection: { kind: "node", key: "A1" } });
 * // "tab=persons&sel=node&key=A1"
 *
 * serializeUrlState({ screen: "app", tab: 3, selection: { kind: "dept", id: 0 } });
 * // "tab=pubs&sel=dept&id=0"
 */
export function serializeUrlState(state: { screen: Screen; tab: TabId; selection: Selection }): string {
  if (state.screen === "menu") return new URLSearchParams({ tab: MENU_SLUG }).toString();

  const params = new URLSearchParams({ tab: TAB_SLUGS[state.tab] });
  const selection = state.selection;

  if (selection?.kind === "node") {
    params.set("sel", "node");
    params.set("key", selection.key);
  } else if (selection?.kind === "edge") {
    params.set("sel", "edge");
    params.set("s", selection.s);
    params.set("t", selection.t);
  } else if (selection?.kind === "dept") {
    params.set("sel", "dept");
    params.set("id", String(selection.id));
  }

  return params.toString();
}

/**
 * Разбирает query-строку адресной строки (например, `location.search`)
 * обратно в `{screen, tab, selection}` — обратная операция к
 * {@link serializeUrlState}, но не идентичная ей 1-в-1: результат ещё и
 * проверяется по реальным `data`.
 *
 * - Query без `tab` вообще (чистый `/`) ИЛИ `tab=start` — меню
 *   (`screen: "menu"`), а не молчаливый откат на вкладку по умолчанию с
 *   пустым query, как было раньше: меню теперь настоящее состояние
 *   приложения, а не отсутствие состояния.
 * - Любой другой нераспознанный слаг (например, `tab=foo`) тоже
 *   откатывается на меню — безопаснее показать выбор, чем угадывать
 *   вкладку по битой ссылке.
 * - Выбор (`sel=node|edge|dept`) ищется в `data`: устаревшая или руками
 *   испорченная ссылка (данные перегенерировали, ключа/пары/id больше нет)
 *   тихо откатывается на `selection: null`, а не приводит к пустой или
 *   битой карточке где-то ниже по цепочке (в features/panels.ts).
 * - Для ребра порядок `s`/`t` в URL не важен (рёбра неориентированы): пара
 *   ищется в обе стороны, а итоговые `s`/`t`/`w` берутся из найденного в
 *   `data` ребра, а не из самой строки URL.
 *
 * @param search - query-строка, с ведущим `"?"` или без него (тот же формат,
 *   что принимает нативный `new URLSearchParams(search)`).
 * @param data - текущие данные графа, по которым проверяется, что выбор из
 *   URL всё ещё существует.
 * @returns Восстановленные `{screen, tab, selection}`, гарантированно
 *   валидные относительно `data` (либо `selection: null`, если ссылка была битой).
 *
 * @example
 * // Пустая строка — меню, ничего не выбрано:
 * parseUrlState("", data);
 * // { screen: "menu", tab: 1, selection: null }
 *
 * // Явное меню — то же самое:
 * parseUrlState("?tab=start", data);
 * // { screen: "menu", tab: 1, selection: null }
 *
 * // Ключ узла реально есть в data — восстанавливаем выбор:
 * parseUrlState("?tab=persons&sel=node&key=A1", data);
 * // { screen: "app", tab: 1, selection: { kind: "node", key: "A1" } }
 *
 * // Ключа "NOPE" в data нет (устаревшая ссылка) — тихий откат на null:
 * parseUrlState("?tab=persons&sel=node&key=NOPE", data);
 * // { screen: "app", tab: 1, selection: null }
 *
 * // Ребро найдено даже при перевёрнутом порядке s/t, вес взят из data:
 * parseUrlState("?tab=persons&sel=edge&s=A2&t=A1", data);
 * // { screen: "app", tab: 1, selection: { kind: "edge", s: "A1", t: "A2", w: 2 } }
 */
export function parseUrlState(search: string, data: GraphData): { screen: Screen; tab: TabId; selection: Selection } {
  const params = new URLSearchParams(search);
  const rawTab = params.get("tab");
  // Индексация по строковому ключу (не по TabId) — noUncheckedIndexedAccess
  // типизирует результат как "TabId | undefined", поэтому сама проверка
  // валидности слага — это проверка на undefined ниже, а не отдельный `in`.
  const tab = rawTab !== null ? SLUG_TO_TAB[rawTab] : undefined;

  if (rawTab === null || rawTab === MENU_SLUG || tab === undefined) {
    return { screen: "menu", tab: 1, selection: null };
  }

  const kind = params.get("sel");
  if (kind === "node") {
    const key = params.get("key");
    if (key !== null && indexByKey(data).has(key)) return { screen: "app", tab, selection: { kind: "node", key } };
  } else if (kind === "edge") {
    const s = params.get("s");
    const t = params.get("t");
    if (s !== null && t !== null) {
      // Рёбра неориентированы — совпадение в любом порядке концов; вес не
      // кладём в URL, а берём заново из data, чтобы не тащить в ссылке
      // производное значение (и не доверять ему, если его подделали).
      const allEdges = [...data.coauth_edges, ...data.repo_edges, ...data.pub_edges];
      const edge = allEdges.find((e) => (e.s === s && e.t === t) || (e.s === t && e.t === s));
      if (edge) return { screen: "app", tab, selection: { kind: "edge", s: edge.s, t: edge.t, w: edge.w } };
    }
  } else if (kind === "dept") {
    const id = Number(params.get("id"));
    if (data.departments.some((dept) => dept.id === id)) return { screen: "app", tab, selection: { kind: "dept", id } };
  }

  return { screen: "app", tab, selection: null };
}
