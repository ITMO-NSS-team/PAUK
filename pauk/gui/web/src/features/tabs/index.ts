import type Sigma from "sigma";
import type { GraphData, PubDetail, RepoDetail } from "../../contracts/graph";
import { t, type Lang, type LocaleKey } from "../../core/i18n";
import type { AppState, Store, TabId } from "../../core/state";
import { authorsTab } from "./authors";
import { pubsTab } from "./pubs";
import { reposTab } from "./repos";
import type { TabModule } from "./types";

const TAB_MODULES: Record<TabId, TabModule> = {
  1: authorsTab,
  2: reposTab,
  3: pubsTab,
};

/** Buttons in index.html carry only `data-tab`, the text comes from i18n. */
const TAB_LABEL_KEYS: Record<TabId, LocaleKey> = {
  1: "tab.authors",
  2: "tab.repos",
  3: "tab.pubs",
};

/**
 * Mounts the list of the active tab; the map follows `store.tab` on its own
 * in map/build.ts.
 *
 * @returns Unmount function.
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
  let activeTab: TabId | null = null;
  let activeLang: Lang | null = null;

  /**
   * Stale selection is cleared by map/build.ts::mountReactiveGraph, which
   * also covers filter changes.
   */
  function activateTab(tabId: TabId): void {
    if (tabId === activeTab) return;

    activeUnmount?.();
    activeTab = tabId;
    activeUnmount = TAB_MODULES[tabId].mount(
      tabContentEl,
      store,
      renderer,
      data,
      pubDetails,
      repoDetails,
    );

    for (const button of buttons) {
      button.classList.toggle("tab-button--active", Number(button.dataset.tab) === tabId);
    }
  }

  function applyLang(lang: Lang): void {
    if (lang === activeLang) return;
    activeLang = lang;

    for (const button of buttons) {
      const tabId = Number(button.dataset.tab) as TabId;
      button.textContent = t(TAB_LABEL_KEYS[tabId], lang);
    }
  }

  function onButtonsClick(event: MouseEvent): void {
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
