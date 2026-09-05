import type { Map as MapLibreMap } from "maplibre-gl";
import type { GraphData } from "../../contracts/graph";
import type { SearchDetail } from "../../contracts/search";
import { t, type Lang, type LocaleKey } from "../../core/i18n";
import type { AppState, Store, TabId } from "../../core/state";
import { authorsTab } from "./authors";
import { pubsTab } from "./pubs";
import { reposTab } from "./repos";
import { searchTab } from "./search";
import type { TabModule } from "./types";

// Вкладку "Здоровье БД" из старого GUI в new_gui не переносим вообще —
// поэтому в TabId для неё нет номера и здесь для неё нет записи
// (см. core/state.ts).
/** Соответствие номера вкладки её модулю {@link TabModule}. */
const TAB_MODULES: Partial<Record<TabId, TabModule>> = {
  1: authorsTab,
  2: reposTab,
  3: pubsTab,
  4: searchTab,
};

/** Какой ключ i18n соответствует подписи кнопки каждой вкладки — статичная разметка кнопок в index.html не хранит текст, только `data-tab`. */
const TAB_LABEL_KEYS: Record<TabId, LocaleKey> = {
  1: "tab.authors",
  2: "tab.repos",
  3: "tab.pubs",
  4: "tab.search",
};

/**
 * Управляет переключением вкладок: слушает клики по кнопкам вкладок и
 * пишет номер в `store.tab`; слушает store и при смене `tab` размонтирует
 * текущую вкладку (`activeUnmount`) и монтирует новую — ровно тот паттерн
 * "unmount текущей, mount новой" из архитектуры, вместо разрастающегося
 * `if/else` в одной функции, как было в старом `main.js` (`setTab()`).
 * Заодно следит за `store.lang`: подписи кнопок переключаются на нужный язык.
 *
 * То, что показывает карта (три разных графа — авторы+соавторство /
 * репозитории+их связи / публикации+их связи), переключается не отсюда:
 * `map/build.ts::mountReactiveGraph()` сама следит за `store.tab` и
 * перерисовывается — `activateTab()` внутри отвечает только за список в
 * сайдбаре, не за карту.
 *
 * @param tabButtonsEl - контейнер с кнопками вкладок (`<nav id="tab-buttons">`), каждая с атрибутом `data-tab`.
 * @param tabContentEl - контейнер, куда монтируется содержимое активной вкладки (`#tab-content`).
 * @param store - Store приложения.
 * @param map - экземпляр карты MapLibre (передаётся дальше в `TabModule.mount()`).
 * @param data - данные графа.
 * @param searchDetails - карта деталей публикаций.
 * @returns Функция размонтирования (unmount) — снимает обработчик кликов по
 *   кнопкам, размонтирует активную вкладку и отписывается от Store.
 */
export function mountTabs(
  tabButtonsEl: HTMLElement,
  tabContentEl: HTMLElement,
  store: Store<AppState>,
  map: MapLibreMap,
  data: GraphData,
  searchDetails: Map<string, SearchDetail>,
): () => void {
  const buttons = tabButtonsEl.querySelectorAll<HTMLButtonElement>("button[data-tab]");

  let activeUnmount: (() => void) | null = null;
  // Отдельно храним, какая вкладка сейчас смонтирована и на каком языке
  // подписаны кнопки, чтобы не пересоздавать список / не переписывать
  // textContent зря, если store поменялся по другой причине (например,
  // изменилось selection).
  let activeTab: TabId | null = null;
  let activeLang: Lang | null = null;

  /**
   * Размонтирует текущую вкладку (если есть) и монтирует новую по номеру
   * `tabId`, обновляя подсветку активной кнопки. Не делает ничего, если
   * `tabId` уже активна.
   *
   * @param tabId - номер вкладки, которую нужно сделать активной.
   */
  function activateTab(tabId: TabId): void {
    if (tabId === activeTab) return;

    activeUnmount?.();
    activeTab = tabId;
    const tabModule = TAB_MODULES[tabId];
    activeUnmount = tabModule ? tabModule.mount(tabContentEl, store, map, data, searchDetails) : null;

    for (const button of buttons) {
      button.classList.toggle("tab-button--active", Number(button.dataset.tab) === tabId);
    }
  }

  /**
   * Переписывает текст всех кнопок вкладок под новый язык. Не делает
   * ничего, если язык не изменился с прошлого вызова.
   *
   * @param lang - язык, на который нужно переключить подписи кнопок.
   */
  function applyLang(lang: Lang): void {
    if (lang === activeLang) return;
    activeLang = lang;

    for (const button of buttons) {
      const tabId = Number(button.dataset.tab) as TabId;
      button.textContent = t(TAB_LABEL_KEYS[tabId], lang);
    }
  }

  /**
   * Обработчик клика по контейнеру кнопок вкладок (делегирование вместо
   * отдельного слушателя на каждую кнопку) — пишет номер вкладки в `store.tab`.
   *
   * @param event - событие клика мыши по `tabButtonsEl` или его потомку.
   */
  function onButtonsClick(event: MouseEvent): void {
    // closest(), а не сравнение event.target напрямую — клик может прийтись
    // на текст внутри кнопки, а не на саму кнопку.
    const button = (event.target as HTMLElement).closest<HTMLButtonElement>("button[data-tab]");
    if (!button?.dataset.tab) return;
    store.set({ tab: Number(button.dataset.tab) as TabId });
  }

  tabButtonsEl.addEventListener("click", onButtonsClick);
  applyLang(store.get().lang);
  activateTab(store.get().tab);
  const unsubscribe = store.subscribe((state) => {
    applyLang(state.lang);
    activateTab(state.tab);
  });

  return () => {
    tabButtonsEl.removeEventListener("click", onButtonsClick);
    activeUnmount?.();
    unsubscribe();
  };
}
