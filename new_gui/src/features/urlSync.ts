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

/**
 * Подключает двустороннюю синхронизацию `{tab, selection}` с адресной
 * строкой браузера:
 * - изменения Store → `history.pushState`/`replaceState` (через
 *   {@link serializeUrlState});
 * - `popstate` (кнопки "назад"/"вперёд" браузера) → обратно в Store (через
 *   {@link parseUrlState}).
 *
 * `pushState` создаёт новую запись в истории только при смене ВКЛАДКИ,
 * `replaceState` используется при смене `selection` в пределах той же
 * вкладки. Иначе клики по узлам/рёбрам (частое, "лёгкое" действие)
 * заваливали бы историю браузера остановками, и "назад" пришлось бы жать
 * по разу на каждый клик, а не для реальной навигации между разделами.
 * Ссылка при этом всё равно всегда полностью описывает и вкладку, и
 * выбор — просто не каждое такое изменение создаёт отдельную остановку в
 * истории.
 *
 * При монтировании функция всегда нормализует текущий URL под то, что
 * реально показано (`history.replaceState`) — на чистом `/` это означает
 * явную запись `?tab=start`: меню (features/start.ts) — настоящее состояние
 * приложения (`AppState.screen`), а не "пока никакого состояния нет".
 *
 * @param store - Store приложения.
 * @param data - данные графа, нужны {@link parseUrlState} для проверки, что
 *   выбор из URL при возврате назад/вперёд всё ещё существует.
 * @returns Функция отписки (unmount) — снимает обработчик `popstate` и отписывается от Store.
 */
export function mountUrlSync(store: Store<AppState>, data: GraphData): () => void {
  // Пока true, реакция на popstate не должна сама снова писать в историю —
  // иначе "назад" тут же перекрывалось бы новой записью "вперёд". Аналог
  // старого _routingFromPop в main.js.
  let applyingFromHistory = false;

  /**
   * Обрабатывает событие `popstate` (кнопки "назад"/"вперёд" браузера):
   * разбирает уже обновлённый браузером `location.search` через
   * {@link parseUrlState} и применяет результат к Store, временно
   * выставляя `applyingFromHistory`, чтобы вызванный этим `store.set()`
   * не запустил повторную запись в историю (см. подписку ниже).
   */
  function onPopState(): void {
    applyingFromHistory = true;
    store.set(parseUrlState(location.search, data));
    applyingFromHistory = false;
  }

  window.addEventListener("popstate", onPopState);

  // Нормализует URL сразу при монтировании: main.ts к этому моменту уже
  // применил parseUrlState к начальному состоянию (в т.ч. на чистом "/" —
  // в screen: "menu"), здесь просто фиксируем результат как "?tab=start"
  // или "?tab=<слаг>...", а не оставляем пустой query.
  history.replaceState(null, "", `?${serializeUrlState(store.get())}`);

  let prev = store.get();
  const unsubscribe = store.subscribe((state) => {
    if (state.screen === prev.screen && state.tab === prev.tab && state.selection === prev.selection) return;
    // Смена экрана (меню ↔ приложение) или вкладки — это реальная
    // навигация, заслуживающая отдельной записи в истории; смена selection
    // в пределах того же экрана/вкладки — лёгкое действие (клик по узлу),
    // не должна заваливать историю остановками.
    const majorChange = state.screen !== prev.screen || state.tab !== prev.tab;
    prev = state;
    if (applyingFromHistory) return;

    const url = `?${serializeUrlState(state)}`;
    if (majorChange) history.pushState(null, "", url);
    else history.replaceState(null, "", url);
  });

  return () => {
    window.removeEventListener("popstate", onPopState);
    unsubscribe();
  };
}
