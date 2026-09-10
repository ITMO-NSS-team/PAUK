import type Sigma from "sigma";
import type { GraphData, PubDetail, RepoDetail } from "../../contracts/graph";
import { t, type Lang, type LocaleKey } from "../../core/i18n";
import type { AppState, Store, TabId } from "../../core/state";
import { authorsTab } from "./authors";
import { pubsTab } from "./pubs";
import { reposTab } from "./repos";
import type { TabModule } from "./types";

// Вкладку "Здоровье БД" из старого GUI в new_gui не переносим вообще —
// поэтому в TabId для неё нет номера и здесь для неё нет записи
// (см. core/state.ts). Вкладка "Поиск" убрана временно — переезжает в
// левую панель каждой вкладки вместе с фильтрами, когда решится, как
// именно (см. память проекта); сама логика поиска осталась в
// features/search/index.ts, просто не смонтирована как отдельная вкладка.
/** Соответствие номера вкладки её модулю {@link TabModule}. */
const TAB_MODULES: Record<TabId, TabModule> = {
  1: authorsTab,
  2: reposTab,
  3: pubsTab,
};

/** Какой ключ i18n соответствует подписи кнопки каждой вкладки — статичная разметка кнопок в index.html не хранит текст, только `data-tab`. */
const TAB_LABEL_KEYS: Record<TabId, LocaleKey> = {
  1: "tab.authors",
  2: "tab.repos",
  3: "tab.pubs",
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
 * @param renderer - Sigma-рендерер (передаётся дальше в `TabModule.mount()`).
 * @param data - данные графа.
 * @param pubDetails - карта деталей публикаций.
 * @param repoDetails - карта описаний/владельцев/ссылок репозиториев — сейчас
 *   ни одна из смонтированных вкладок её не читает (см. `./types.ts::TabModule`).
 * @returns Функция размонтирования (unmount) — снимает обработчик кликов по
 *   кнопкам, размонтирует активную вкладку и отписывается от Store.
 */
export function mountTabs(
  tabButtonsEl: HTMLElement,
  tabContentEl: HTMLElement,
  store: Store<AppState>,
  renderer: Sigma,
  data: GraphData,
  pubDetails: Map<string, PubDetail>,
  repoDetails: Map<string, RepoDetail>,
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
   * Не трогает `store.selection` сама — обнулять выбор, ставший чужим для
   * новой вкладки (после клика map/build.ts::populateGraph пересобирает
   * граф под другой набор сущностей), это забота
   * map/build.ts::mountReactiveGraph: она проверяет это при ЛЮБОЙ причине
   * пересборки графа (смена вкладки ИЛИ фильтров), а не только здесь.
   *
   * @param tabId - номер вкладки, которую нужно сделать активной.
   */
  function activateTab(tabId: TabId): void {
    if (tabId === activeTab) return;

    activeUnmount?.();
    activeTab = tabId;
    // TAB_MODULES теперь Record, не Partial<Record<...>> — каждый TabId
    // гарантированно имеет модуль, отдельной проверки на undefined не нужно.
    activeUnmount = TAB_MODULES[tabId].mount(tabContentEl, store, renderer, data, pubDetails, repoDetails);

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
   * Не трогает `selection` сама — она может тянуть за собой ключ, которого
   * больше нет в новой вкладке (после клика map/build.ts::populateGraph
   * пересобирает граф под другой набор сущностей), но обнулять устаревший
   * выбор — забота map/build.ts::mountReactiveGraph (единая проверка
   * `selectionExistsIn()` при ЛЮБОЙ причине пересборки графа — смене
   * вкладки ИЛИ фильтров, не только клика по кнопке).
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
