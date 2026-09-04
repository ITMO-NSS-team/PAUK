import type { Map as MapLibreMap } from "maplibre-gl";
import type { GraphData } from "../../contracts/graph";
import type { AppState, Store, TabId } from "../../core/state";
import { authorsTab } from "./authors";
import { pubsTab } from "./pubs";
import { reposTab } from "./repos";
import type { TabModule } from "./types";

// Кнопок для вкладок 4 (поиск) и 5 (здоровье БД) в разметке пока нет —
// их ещё не с чем связывать, добавим вместе с самими вкладками.
const TAB_MODULES: Partial<Record<TabId, TabModule>> = {
  1: authorsTab,
  2: reposTab,
  3: pubsTab,
};

/**
 * Управляет переключением вкладок: слушает клики по кнопкам вкладок и
 * пишет номер в store.tab; слушает store и при смене tab размонтирует
 * текущую вкладку (activeUnmount) и монтирует новую — ровно тот паттерн
 * "unmount текущей, mount новой" из архитектуры, вместо разрастающегося
 * if/else в одной функции, как было в старом main.js (setTab()).
 */
export function mountTabs(
  tabButtonsEl: HTMLElement,
  tabContentEl: HTMLElement,
  store: Store<AppState>,
  map: MapLibreMap,
  data: GraphData,
): () => void {
  let activeUnmount: (() => void) | null = null;
  // Отдельно храним, какая вкладка сейчас смонтирована, чтобы не
  // пересоздавать список заново, если store поменялся по другой причине
  // (например, изменилось selection), а не из-за смены вкладки.
  let activeTab: TabId | null = null;

  function activate(tabId: TabId): void {
    if (tabId === activeTab) return;

    activeUnmount?.();
    activeTab = tabId;
    const tabModule = TAB_MODULES[tabId];
    // Для 4/5 модуля пока нет — контейнер просто остаётся пустым, это не ошибка.
    activeUnmount = tabModule ? tabModule.mount(tabContentEl, store, map, data) : null;

    for (const button of tabButtonsEl.querySelectorAll<HTMLButtonElement>("button[data-tab]")) {
      button.classList.toggle("tab-button--active", Number(button.dataset.tab) === tabId);
    }
  }

  function onButtonsClick(event: MouseEvent): void {
    // closest(), а не сравнение event.target напрямую — клик может прийтись
    // на текст внутри кнопки, а не на саму кнопку.
    const button = (event.target as HTMLElement).closest<HTMLButtonElement>("button[data-tab]");
    if (!button?.dataset.tab) return;
    store.set({ tab: Number(button.dataset.tab) as TabId });
  }

  tabButtonsEl.addEventListener("click", onButtonsClick);
  activate(store.get().tab);
  const unsubscribe = store.subscribe((state) => activate(state.tab));

  return () => {
    tabButtonsEl.removeEventListener("click", onButtonsClick);
    activeUnmount?.();
    unsubscribe();
  };
}
