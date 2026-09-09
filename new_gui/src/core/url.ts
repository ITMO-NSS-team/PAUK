// Слой "core" — чистая (без DOM/history) сериализация {tab, selection} в
// параметры URL и обратно. Сознательно НЕ включает lang и filters — как и в
// старом GUI (main.js: _pushUrl/_replaceUrl носили только tab/kind/key/id),
// это состояние интерфейса самого пользователя, а не то, чем он делится
// ссылкой. Живую синхронизацию (history.pushState/popstate) делает
// features/urlSync.ts — здесь только преобразование данных в обе стороны,
// поэтому оно тестируется без единого DOM-события.

import type { GraphData } from "../contracts/graph";
import { indexByKey } from "./data";
import type { Selection, TabId } from "./state";

/**
 * Проверяет, что число — это одно из допустимых значений {@link TabId} (1-4).
 * Используется как type guard в {@link parseUrlState}, чтобы после проверки
 * TypeScript сам знал, что переменная имеет тип `TabId`, без приведения `as`.
 *
 * @param value - число, полученное из URL (`Number(params.get("tab"))`).
 * @returns `true`, если `value` — это 1, 2, 3 или 4.
 *
 * @example
 * isTabId(2);   // true
 * isTabId(5);   // false — вкладки 5 ("Здоровье БД") в new_gui нет
 * isTabId(NaN); // false — например, если в URL был tab=abc
 */
function isTabId(value: number): value is TabId {
  return value === 1 || value === 2 || value === 3 || value === 4;
}

/**
 * Сериализует текущие `tab`/`selection` в строку параметров URL — ровно то,
 * что дальше передаётся в `history.pushState`/`replaceState` (см.
 * features/urlSync.ts). Вес ребра (`w`) сознательно не кладётся в
 * результат — при разборе ({@link parseUrlState}) он заново берётся из
 * `data`, а не из URL, чтобы ссылка не могла "соврать" о весе.
 *
 * @param state - минимальный срез состояния приложения, который стоит
 *   отражать в адресной строке: активная вкладка и текущий выбор.
 * @returns Строка вида `"tab=1"` или `"tab=1&sel=node&key=A1"` — без
 *   ведущего `"?"` (его добавляет вызывающий код перед `pushState`/`replaceState`).
 *
 * @example
 * serializeUrlState({ tab: 1, selection: null });
 * // "tab=1"
 *
 * serializeUrlState({ tab: 1, selection: { kind: "node", key: "A1" } });
 * // "tab=1&sel=node&key=A1"
 *
 * serializeUrlState({ tab: 1, selection: { kind: "edge", s: "A1", t: "A2", w: 2 } });
 * // "tab=1&sel=edge&s=A1&t=A2" — обратите внимание, w=2 в строку не попал
 *
 * serializeUrlState({ tab: 4, selection: { kind: "dept", id: 0 } });
 * // "tab=4&sel=dept&id=0"
 */
export function serializeUrlState(state: { tab: TabId; selection: Selection }): string {
  const params = new URLSearchParams({ tab: String(state.tab) });
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
 * обратно в `{tab, selection}` — обратная операция к {@link serializeUrlState},
 * но не идентичная ей 1-в-1: результат ещё и проверяется по реальным `data`.
 *
 * - `tab` вне диапазона 1-4 (или вообще не число, например `tab=abc`)
 *   откатывается на 1, а не бросает исключение и не оставляет `NaN`.
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
 * @returns Восстановленные `{tab, selection}`, гарантированно валидные
 *   относительно `data` (либо `selection: null`, если ссылка была битой).
 *
 * @example
 * // Пустая строка — вкладка 1 по умолчанию, ничего не выбрано:
 * parseUrlState("", data);
 * // { tab: 1, selection: null }
 *
 * // Ключ узла реально есть в data — восстанавливаем выбор:
 * parseUrlState("?tab=1&sel=node&key=A1", data);
 * // { tab: 1, selection: { kind: "node", key: "A1" } }
 *
 * // Ключа "NOPE" в data нет (устаревшая ссылка) — тихий откат на null:
 * parseUrlState("?tab=1&sel=node&key=NOPE", data);
 * // { tab: 1, selection: null }
 *
 * // Ребро найдено даже при перевёрнутом порядке s/t, вес взят из data:
 * parseUrlState("?tab=1&sel=edge&s=A2&t=A1", data);
 * // { tab: 1, selection: { kind: "edge", s: "A1", t: "A2", w: 2 } }
 */
export function parseUrlState(search: string, data: GraphData): { tab: TabId; selection: Selection } {
  const params = new URLSearchParams(search);
  const rawTab = Number(params.get("tab"));
  const tab = isTabId(rawTab) ? rawTab : 1;

  const kind = params.get("sel");
  if (kind === "node") {
    const key = params.get("key");
    if (key !== null && indexByKey(data).has(key)) return { tab, selection: { kind: "node", key } };
  } else if (kind === "edge") {
    const s = params.get("s");
    const t = params.get("t");
    if (s !== null && t !== null) {
      // Рёбра неориентированы — совпадение в любом порядке концов; вес не
      // кладём в URL, а берём заново из data, чтобы не тащить в ссылке
      // производное значение (и не доверять ему, если его подделали).
      const allEdges = [...data.coauth_edges, ...data.repo_edges, ...data.pub_edges];
      const edge = allEdges.find((e) => (e.s === s && e.t === t) || (e.s === t && e.t === s));
      if (edge) return { tab, selection: { kind: "edge", s: edge.s, t: edge.t, w: edge.w } };
    }
  } else if (kind === "dept") {
    const id = Number(params.get("id"));
    if (data.departments.some((dept) => dept.id === id)) return { tab, selection: { kind: "dept", id } };
  }

  return { tab, selection: null };
}
