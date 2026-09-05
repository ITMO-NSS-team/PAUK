// Слой "features" — двусторонняя синхронизация {tab, selection} с адресной
// строкой браузера: изменения Store уходят в history.pushState/replaceState,
// а popstate (кнопки назад/вперёд) возвращаются обратно в Store. Сама
// сериализация — в core/url.ts, здесь только live-часть (DOM/history).
//
// pushState только на смену ВКЛАДКИ, replaceState — на смену selection в
// пределах той же вкладки. Иначе клики по узлам/рёбрам (частое, "лёгкое"
// действие) заваливали бы историю браузера остановками, и "назад" пришлось
// бы жать по разу на каждый клик, а не для реальной навигации. Ссылка при
// этом всё равно всегда полностью описывает и вкладку, и выбор — просто
// не каждое такое изменение создаёт отдельную остановку в истории.

import type { GraphData } from "../contracts/graph";
import { parseUrlState, serializeUrlState } from "../core/url";
import type { AppState, Store } from "../core/state";

export function mountUrlSync(store: Store<AppState>, data: GraphData): () => void {
  // Пока true, реакция на popstate не должна сама снова писать в историю —
  // иначе "назад" тут же перекрывалось бы новой записью "вперёд". Аналог
  // старого _routingFromPop в main.js.
  let applyingFromHistory = false;

  function onPopState(): void {
    applyingFromHistory = true;
    store.set(parseUrlState(location.search, data));
    applyingFromHistory = false;
  }

  window.addEventListener("popstate", onPopState);

  // Нормализует URL сразу при монтировании: если страница открылась с
  // частичным/битым query (или вообще без него), в адресной строке должно
  // остаться то, что реально показано (main.ts к этому моменту уже применил
  // parseUrlState к начальному состоянию — здесь просто фиксируем результат).
  history.replaceState(null, "", `?${serializeUrlState(store.get())}`);

  let prev = store.get();
  const unsubscribe = store.subscribe((state) => {
    if (state.tab === prev.tab && state.selection === prev.selection) return;
    const tabChanged = state.tab !== prev.tab;
    prev = state;
    if (applyingFromHistory) return;

    const url = `?${serializeUrlState(state)}`;
    if (tabChanged) history.pushState(null, "", url);
    else history.replaceState(null, "", url);
  });

  return () => {
    window.removeEventListener("popstate", onPopState);
    unsubscribe();
  };
}
