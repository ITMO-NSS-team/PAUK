import { requireElement } from "../core/dom";
import { t } from "../core/i18n";
import type { AppState, Store } from "../core/state";

/** Panels overlay the map on narrow screens, preserving Sigma's dimensions. */
export function mountMobilePanels(store: Store<AppState>): () => void {
  const app = requireElement("app");
  const list = requireElement("mobile-list");
  const close = requireElement("mobile-close");
  const backdrop = requireElement("mobile-backdrop");
  let pane: "sidebar" | null = null;
  let previousSelection = store.get().selection;

  function render(): void {
    const state = store.get();
    list.textContent = t("mobile.list", state.lang);
    close.textContent = t("mobile.close", state.lang);
    backdrop.setAttribute("aria-label", t("mobile.close", state.lang));
    list.setAttribute("aria-expanded", String(pane === "sidebar"));
    app.dataset.mobilePane = pane ?? "";
    close.hidden = pane === null;
  }
  function setPane(next: typeof pane): void {
    pane = next;
    render();
  }
  const showList = () => setPane(pane === "sidebar" ? null : "sidebar");
  const hide = () => setPane(null);
  function keydown(event: KeyboardEvent): void {
    if (event.key === "Escape") hide();
  }
  list.addEventListener("click", showList);
  close.addEventListener("click", hide);
  backdrop.addEventListener("click", hide);
  document.addEventListener("keydown", keydown);
  render();
  const unsubscribe = store.subscribe((state) => {
    if (state.screen === "menu") pane = null;
    else if (state.selection !== previousSelection && state.selection !== null) pane = "sidebar";
    previousSelection = state.selection;
    render();
  });
  return () => {
    unsubscribe();
    list.removeEventListener("click", showList);
    close.removeEventListener("click", hide);
    backdrop.removeEventListener("click", hide);
    document.removeEventListener("keydown", keydown);
  };
}
