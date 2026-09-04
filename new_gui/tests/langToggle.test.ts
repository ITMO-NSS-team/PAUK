import type { Map as MapLibreMap } from "maplibre-gl";
import { describe, expect, it, vi } from "vitest";
import { loadSampleGraphData } from "../src/core/data";
import { Store, type AppState } from "../src/core/state";
import { mountLangToggle } from "../src/features/langToggle";

function initialState(): AppState {
  return {
    tab: 1,
    lang: "ru",
    selection: null,
    filters: { minCoauth: 1, minPubAuthors: 1, yearMax: 2026 },
  };
}

/** setData — единственный метод карты, который использует refreshGraphForTab() внутри mountLangToggle. */
function fakeMapWithSource(): { map: MapLibreMap; setData: ReturnType<typeof vi.fn> } {
  const setData = vi.fn();
  const map = { getSource: () => ({ setData }) } as unknown as MapLibreMap;
  return { map, setData };
}

describe("mountLangToggle", () => {
  it("клик переключает store.lang и просит карту пересобрать подписи узлов", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    const { map, setData } = fakeMapWithSource();

    const button = document.createElement("button");
    button.id = "lang-toggle";
    document.body.appendChild(button);

    try {
      mountLangToggle(store, map, data);
      // Кнопка показывает язык, НА КОТОРЫЙ переключит клик, а не текущий.
      expect(button.textContent).toBe("EN");

      button.click();
      expect(store.get().lang).toBe("en");
      expect(button.textContent).toBe("RU");
      // Обновляются оба источника (узлы и рёбра текущей вкладки), поэтому 2 вызова, не 1.
      expect(setData).toHaveBeenCalledTimes(2);

      button.click();
      expect(store.get().lang).toBe("ru");
    } finally {
      button.remove();
    }
  });
});
