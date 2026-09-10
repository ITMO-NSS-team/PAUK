import { describe, expect, it } from "vitest";
import { mountFilters } from "../src/features/filters";
import { Store, type AppState } from "../src/core/state";

function initialState(overrides: Partial<AppState> = {}): AppState {
  return {
    screen: "app",
    tab: 1,
    lang: "ru",
    selection: null,
    filters: { minCoauth: 1, minSharedAuthors: 1, yearMax: 2026, showNoDeptAuthors: true, showNoDeptPubs: true },
    ...overrides,
  };
}

describe("mountFilters", () => {
  let container: HTMLElement;

  function withContainer<T>(run: () => T): T {
    container = document.createElement("div");
    container.id = "filter-bar";
    document.body.appendChild(container);
    try {
      return run();
    } finally {
      container.remove();
    }
  }

  it("на вкладке 1 показывает один регулятор — порог соавторства", () => {
    withContainer(() => {
      const store = new Store<AppState>(initialState());
      mountFilters(store);

      expect(container.hidden).toBe(false);
      const inputs = container.querySelectorAll("input[type='range']");
      expect(inputs).toHaveLength(1);
      expect((inputs[0] as HTMLInputElement).value).toBe("1");
    });
  });

  it("на вкладке 3 показывает два регулятора — общих авторов и год", () => {
    withContainer(() => {
      const store = new Store<AppState>(initialState({ tab: 3 }));
      mountFilters(store);

      expect(container.querySelectorAll("input[type='range']")).toHaveLength(2);
    });
  });

  it("на вкладке 2 (репозитории) регуляторов нет вообще, панель скрыта", () => {
    withContainer(() => {
      const store = new Store<AppState>(initialState({ tab: 2 }));
      mountFilters(store);

      expect(container.hidden).toBe(true);
      expect(container.children).toHaveLength(0);
    });
  });

  it("движение ползунка пишет новое значение в store.filters", () => {
    withContainer(() => {
      const store = new Store<AppState>(initialState());
      mountFilters(store);

      const input = container.querySelector("input[type='range']") as HTMLInputElement;
      input.value = "7";
      input.dispatchEvent(new Event("input"));

      expect(store.get().filters.minCoauth).toBe(7);
    });
  });

  it("переключение на вкладку без фильтров скрывает панель и очищает разметку", () => {
    withContainer(() => {
      const store = new Store<AppState>(initialState());
      mountFilters(store);

      store.set({ tab: 2 });

      expect(container.hidden).toBe(true);
      expect(container.children).toHaveLength(0);
    });
  });

  it("на вкладках 1 и 3 есть чекбокс «показывать без департамента»", () => {
    withContainer(() => {
      const store = new Store<AppState>(initialState());
      mountFilters(store);
      expect(container.querySelectorAll("input[type='checkbox']")).toHaveLength(1);

      store.set({ tab: 3 });
      expect(container.querySelectorAll("input[type='checkbox']")).toHaveLength(1);
    });
  });

  it("снятие чекбокса «без департамента» пишет false в соответствующее поле filters", () => {
    withContainer(() => {
      const store = new Store<AppState>(initialState());
      mountFilters(store);

      const checkbox = container.querySelector("input[type='checkbox']") as HTMLInputElement;
      checkbox.checked = false;
      checkbox.dispatchEvent(new Event("change"));

      expect(store.get().filters.showNoDeptAuthors).toBe(false);
    });
  });
});
