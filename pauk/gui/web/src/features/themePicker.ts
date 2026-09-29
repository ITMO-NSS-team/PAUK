import { requireElement } from "../core/dom";
import type { AppState, Store } from "../core/state";
import { THEMES } from "../core/themes";

/** @returns Unsubscribe function. */
export function mountThemePicker(store: Store<AppState>): () => void {
  const container = requireElement("menu-theme");
  const buttons = THEMES.map((theme) => {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.theme = theme.id;
    button.addEventListener("click", () => store.set({ theme: theme.id }));
    container.appendChild(button);
    return { theme, button };
  });

  function render(state: AppState): void {
    for (const { theme, button } of buttons) {
      button.textContent = theme.name[state.lang];
      button.classList.toggle("menu-lang--active", theme.id === state.theme);
    }
  }

  render(store.get());
  return store.subscribe(render);
}
