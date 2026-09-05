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

function isTabId(value: number): value is TabId {
  return value === 1 || value === 2 || value === 3 || value === 4;
}

/** Параметры URL под текущие tab/selection — ровно то, что кладут pushState/replaceState. */
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
 * Разбирает query-строку в {tab, selection}. tab вне 1-4 откатывается на 1.
 * Выбор проверяется по реальным data — устаревшая или руками испорченная
 * ссылка (данные перегенерировали, ключа больше нет) откатывается на null,
 * а не приводит к пустой/битой карточке ниже по цепочке.
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
