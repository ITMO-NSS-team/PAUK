import { t, type LocaleKey } from "../../core/i18n";
import type { AppState, Store, TabId } from "../../core/state";

/** Buttons in index.html carry only `data-tab`, the text comes from i18n. */
const TAB_LABEL_KEYS: Record<TabId, LocaleKey> = {
  1: "tab.authors",
  2: "tab.repos",
  3: "tab.pubs",
};

export function mountTabs(tabButtonsEl: HTMLElement, store: Store<AppState>): () => void {
  const buttons = tabButtonsEl.querySelectorAll<HTMLButtonElement>("button[data-tab]");

  function render(state: Readonly<AppState>): void {
    for (const button of buttons) {
      const tab = Number(button.dataset.tab) as TabId;
      button.textContent = t(TAB_LABEL_KEYS[tab], state.lang);
      button.classList.toggle("tab-button--active", tab === state.tab);
      button.setAttribute("aria-pressed", String(tab === state.tab));
    }
  }

  function onButtonsClick(event: MouseEvent): void {
    const button = (event.target as HTMLElement).closest<HTMLButtonElement>("button[data-tab]");
    if (!button?.dataset.tab) return;
    store.set({ tab: Number(button.dataset.tab) as TabId });
  }

  tabButtonsEl.addEventListener("click", onButtonsClick);
  render(store.get());
  const unsubscribe = store.subscribe(render);
  return () => {
    tabButtonsEl.removeEventListener("click", onButtonsClick);
    unsubscribe();
  };
}
