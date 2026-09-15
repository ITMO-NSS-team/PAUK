import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FILTER_CONFIG } from "../src/core/config";
import { mountFilters } from "../src/features/filters";
import { Store, type AppState } from "../src/core/state";

function initialState(overrides: Partial<AppState> = {}): AppState {
  return {
    screen: "app",
    tab: 1,
    lang: "ru",
    selection: null,
    filters: {
      minCoauth: 1,
      minSharedAuthors: 1,
      yearMax: 2026,
      showNoDeptAuthors: true,
      showNoDeptPubs: true,
      edgeZoomThreshold: 0.2,
      showRegions: { 1: false, 2: false, 3: false },
      regionZoomThreshold: 0.25,
      regionMinNodes: 10,
    },
    ...overrides,
  };
}

describe("mountFilters", () => {
  let container: HTMLElement;

  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  function checkboxByLabel(text: string): HTMLInputElement | null {
    const row = [...container.querySelectorAll(".filter-row")].find(
      (el) => el.querySelector(".filter-row__label")?.textContent === text,
    );
    return row?.querySelector<HTMLInputElement>("input[type='checkbox']") ?? null;
  }

  function withContainer<T>(run: () => T): T {
    container = document.createElement("div");
    container.id = "filter-bar";
    document.body.appendChild(container);
    const sectionLabel = document.createElement("div");
    sectionLabel.id = "filters-section-label";
    document.body.appendChild(sectionLabel);
    try {
      return run();
    } finally {
      container.remove();
      sectionLabel.remove();
    }
  }

  it("подписывает секцию сайдбара («Фильтры»/«Filters») под текущий язык", () => {
    withContainer(() => {
      const store = new Store<AppState>(initialState({ lang: "ru" }));
      mountFilters(store);
      expect(document.getElementById("filters-section-label")?.textContent).toBe("Фильтры");

      store.set({ lang: "en" });
      expect(document.getElementById("filters-section-label")?.textContent).toBe("Filters");
    });
  });

  it("на вкладке 1: зум рёбер (общий), порог соавторства, затем два общих регулятора регионов", () => {
    withContainer(() => {
      const store = new Store<AppState>(initialState());
      mountFilters(store);

      expect(container.hidden).toBe(false);
      const inputs = container.querySelectorAll("input[type='range']");
      expect(inputs).toHaveLength(4);
      expect((inputs[0] as HTMLInputElement).value).toBe("0.2"); // зум рёбер — первым, до вкладко-специфичных
      expect((inputs[1] as HTMLInputElement).value).toBe("1"); // порог соавторства
    });
  });

  it("на вкладке 3: зум рёбер, общих авторов, год и два регулятора регионов", () => {
    withContainer(() => {
      const store = new Store<AppState>(initialState({ tab: 3 }));
      mountFilters(store);

      expect(container.querySelectorAll("input[type='range']")).toHaveLength(5);
    });
  });

  it("на вкладке 2 (репозитории) только общие регуляторы: зум рёбер и регионы, панель не скрыта", () => {
    withContainer(() => {
      const store = new Store<AppState>(initialState({ tab: 2 }));
      mountFilters(store);

      expect(container.hidden).toBe(false);
      expect(container.querySelectorAll("input[type='range']")).toHaveLength(3);
      expect(container.querySelectorAll("input[type='checkbox']")).toHaveLength(1); // только «Регионы департаментов»
    });
  });

  it("движение вкладко-специфичного ползунка пишет новое значение в store.filters — с задержкой (debounce), не мгновенно", () => {
    withContainer(() => {
      const store = new Store<AppState>(initialState());
      mountFilters(store);

      // [1] — второй range-инпут, первый ([0]) теперь общий регулятор
      // зума рёбер (см. тест выше про порядок строк).
      const input = container.querySelectorAll("input[type='range']")[1] as HTMLInputElement;
      input.value = "7";
      input.dispatchEvent(new Event("input"));

      // Подпись значения рядом с ползунком обновляется сразу — это просто
      // DOM-текст, не тормозит и не должно ждать debounce. [1] — та же
      // строка, что и сам ползунок выше (не строка общего зума рёбер).
      expect(container.querySelectorAll(".filter-row__value")[1]?.textContent).toBe("7");
      expect(store.get().filters.minCoauth).toBe(1); // ещё не применилось

      vi.advanceTimersByTime(FILTER_CONFIG.debounceMs - 1);
      expect(store.get().filters.minCoauth).toBe(1); // всё ещё не применилось — чуть-чуть не хватило

      vi.advanceTimersByTime(1);
      expect(store.get().filters.minCoauth).toBe(7); // применилось ровно через debounceMs
    });
  });

  it("быстрое перетаскивание ползунка (много тиков подряд) применяет ТОЛЬКО последнее значение, не каждый тик", () => {
    withContainer(() => {
      const store = new Store<AppState>(initialState());
      mountFilters(store);

      const input = container.querySelectorAll("input[type='range']")[1] as HTMLInputElement;
      // Имитация перетаскивания — несколько "input" подряд, каждый раньше,
      // чем истёк debounceMs предыдущего: каждое новое движение сбрасывает
      // отсчёт таймера, применяется только значение, на котором пользователь
      // реально остановился, а не промежуточные тики (иначе на реальных
      // данных перетаскивание гоняло бы полную пересборку графа на каждый
      // пиксель и лагало — прямая жалоба).
      for (const value of [2, 3, 4, 5, 6, 7]) {
        input.value = String(value);
        input.dispatchEvent(new Event("input"));
        vi.advanceTimersByTime(FILTER_CONFIG.debounceMs - 1);
      }
      expect(store.get().filters.minCoauth).toBe(1); // ни один промежуточный тик не применился

      vi.advanceTimersByTime(1);
      expect(store.get().filters.minCoauth).toBe(7); // применилось только последнее значение
    });
  });

  it("движение общего ползунка зума рёбер пишет edgeZoomThreshold независимо от вкладки", () => {
    withContainer(() => {
      const store = new Store<AppState>(initialState({ tab: 2 })); // вкладка без своих фильтров
      mountFilters(store);

      const input = container.querySelector("input[type='range']") as HTMLInputElement;
      input.value = "0.1";
      input.dispatchEvent(new Event("input"));
      vi.advanceTimersByTime(FILTER_CONFIG.debounceMs);

      expect(store.get().filters.edgeZoomThreshold).toBe(0.1);
    });
  });

  it("переключение вкладки перестраивает вкладко-специфичные регуляторы, но общий зум рёбер остаётся", () => {
    withContainer(() => {
      const store = new Store<AppState>(initialState());
      mountFilters(store);

      store.set({ tab: 2 });

      expect(container.hidden).toBe(false);
      // Общие для всех вкладок: зум рёбер, зум регионов, минимум узлов в регионе.
      expect(container.querySelectorAll("input[type='range']")).toHaveLength(3);
    });
  });

  it("на вкладках 1 и 3 есть чекбокс «показывать без департамента», на 2 — нет", () => {
    withContainer(() => {
      const store = new Store<AppState>(initialState());
      mountFilters(store);
      expect(checkboxByLabel("Показывать без департамента")).not.toBeNull();

      store.set({ tab: 3 });
      expect(checkboxByLabel("Показывать без департамента")).not.toBeNull();

      store.set({ tab: 2 });
      expect(checkboxByLabel("Показывать без департамента")).toBeNull();
    });
  });

  it("снятие чекбокса «без департамента» пишет false в соответствующее поле filters", () => {
    withContainer(() => {
      const store = new Store<AppState>(initialState());
      mountFilters(store);

      const checkbox = checkboxByLabel("Показывать без департамента");
      if (!checkbox) throw new Error("чекбокс «без департамента» должен быть на вкладке авторов");
      checkbox.checked = false;
      checkbox.dispatchEvent(new Event("change"));

      expect(store.get().filters.showNoDeptAuthors).toBe(false);
    });
  });
  it("чекбокс «Регионы департаментов» есть на всех вкладках и переключает только текущую", () => {
    withContainer(() => {
      const store = new Store<AppState>(
        initialState({
          tab: 3,
          filters: { ...initialState().filters, showRegions: { 1: true, 2: false, 3: true } },
        }),
      );
      mountFilters(store);

      const checkbox = checkboxByLabel("Регионы департаментов");
      expect(checkbox?.checked).toBe(true);
      if (!checkbox) throw new Error("чекбокс регионов должен быть на вкладке публикаций");
      checkbox.checked = false;
      checkbox.dispatchEvent(new Event("change"));
      expect(store.get().filters.showRegions).toEqual({ 1: true, 2: false, 3: false });

      store.set({ tab: 2 });
      expect(checkboxByLabel("Регионы департаментов")?.checked).toBe(false);
    });
  });

  it("ползунки регионов пишут regionZoomThreshold и regionMinNodes, диапазон зума рёбер не заходит в диапазон регионов", () => {
    withContainer(() => {
      const store = new Store<AppState>(initialState());
      mountFilters(store);

      const ranges = [...container.querySelectorAll<HTMLInputElement>("input[type='range']")];
      const [edgeZoom] = ranges;
      const [regionZoom, minNodes] = ranges.slice(-2); // регионы — последними, после фильтров вкладки
      expect(Number(edgeZoom?.max)).toBeLessThanOrEqual(Number(regionZoom?.min));

      if (!regionZoom || !minNodes) throw new Error("ползунки регионов должны быть в фильтрах");
      regionZoom.value = "0.4";
      regionZoom.dispatchEvent(new Event("input"));
      minNodes.value = "25";
      minNodes.dispatchEvent(new Event("input"));
      vi.advanceTimersByTime(FILTER_CONFIG.debounceMs);

      expect(store.get().filters.regionZoomThreshold).toBe(0.4);
      expect(store.get().filters.regionMinNodes).toBe(25);
    });
  });
});
