import { requireElement } from "../core/dom";
import { t } from "../core/i18n";
import type { AppState, Store } from "../core/state";

/** Panels overlay the map on narrow screens, preserving Sigma's dimensions. */
export function mountMobilePanels(store: Store<AppState>): () => void {
  const app = requireElement("app");
  const list = requireElement("mobile-list");
  const details = requireElement("mobile-details");
  const close = requireElement("mobile-close");
  const backdrop = requireElement("mobile-backdrop");
  let pane: "sidebar" | "panel" | null = null;
  let previousSelection = store.get().selection;

  function render(): void {
    const state = store.get();
    list.textContent = t("mobile.list", state.lang);
    details.textContent = t("mobile.details", state.lang);
    close.textContent = t("mobile.close", state.lang);
    backdrop.setAttribute("aria-label", t("mobile.close", state.lang));
    list.setAttribute("aria-expanded", String(pane === "sidebar"));
    details.setAttribute("aria-expanded", String(pane === "panel"));
    app.dataset.mobilePane = pane ?? "";
    close.hidden = pane === null;
  }
  function setPane(next: typeof pane): void {
    pane = next;
    render();
  }
  const showList = () => setPane(pane === "sidebar" ? null : "sidebar");
  const showDetails = () => setPane(pane === "panel" ? null : "panel");
  const hide = () => setPane(null);
  function keydown(event: KeyboardEvent): void {
    if (event.key === "Escape") hide();
  }
  list.addEventListener("click", showList);
  details.addEventListener("click", showDetails);
  close.addEventListener("click", hide);
  backdrop.addEventListener("click", hide);
  document.addEventListener("keydown", keydown);
  render();
  const unsubscribe = store.subscribe((state) => {
    if (state.screen === "menu") pane = null;
    else if (state.selection !== previousSelection && state.selection !== null) pane = "panel";
    previousSelection = state.selection;
    render();
  });
  return () => {
    unsubscribe();
    list.removeEventListener("click", showList);
    details.removeEventListener("click", showDetails);
    close.removeEventListener("click", hide);
    backdrop.removeEventListener("click", hide);
    document.removeEventListener("keydown", keydown);
  };
}
