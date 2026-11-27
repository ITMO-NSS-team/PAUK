import { requireElement } from "../core/dom";
import type { AppState, Store } from "../core/state";

export function mountPanelToggles(store: Store<AppState>): void {
  for (const button of document.querySelectorAll<HTMLButtonElement>(".pane-toggle")) {
    const id = button.getAttribute("aria-controls");
    if (!id) continue;
    const panel = requireElement(id);
    const left = id === "sidebar";

    function render(): void {
      const open = !panel.classList.contains("is-collapsed");
      const russian = store.get().lang === "ru";
      const name = russian ? (left ? "левую панель" : "правую панель") : (left ? "left panel" : "right panel");
      const action = russian ? (open ? "Скрыть" : "Показать") : (open ? "Hide" : "Show");
      button.title = `${action} ${name}`;
      button.setAttribute("aria-label", button.title);
      button.setAttribute("aria-expanded", String(open));
      button.textContent = left === open ? "‹" : "›";
    }

    button.addEventListener("click", () => {
      panel.classList.toggle("is-collapsed");
      render();
    });
    let previous = store.get();
    store.subscribe((state) => {
      if (!left && state.selection !== null && state.selection !== previous.selection)
        panel.classList.remove("is-collapsed");
      if (state.lang !== previous.lang || state.selection !== previous.selection) render();
      previous = state;
    });
    render();
  }
}
