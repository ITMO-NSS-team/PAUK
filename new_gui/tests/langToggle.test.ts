import { describe, expect, it } from "vitest";
import { Store, type AppState } from "../src/core/state";
import { mountLangToggle } from "../src/features/langToggle";

function initialState(): AppState {
  return {
    tab: 1,
    lang: "ru",
    selection: null,
    filters: { minCoauth: 1, minSharedAuthors: 1, yearMax: 2026 },
  };
}

describe("mountLangToggle", () => {
  it("клик переключает store.lang; сама перерисовка графа — забота mountReactiveGraph, не этой функции", () => {
    const store = new Store<AppState>(initialState());

    const button = document.createElement("button");
    button.id = "lang-toggle";
    document.body.appendChild(button);

    try {
      mountLangToggle(store);
      // Кнопка показывает язык, НА КОТОРЫЙ переключит клик, а не текущий.
      expect(button.textContent).toBe("EN");

      button.click();
      expect(store.get().lang).toBe("en");
      expect(button.textContent).toBe("RU");

      button.click();
      expect(store.get().lang).toBe("ru");
    } finally {
      button.remove();
    }
  });
});
