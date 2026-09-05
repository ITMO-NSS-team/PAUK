import { beforeEach, describe, expect, it } from "vitest";
import { loadSampleGraphData } from "../src/core/data";
import { Store, type AppState } from "../src/core/state";
import { mountUrlSync } from "../src/features/urlSync";

function initialState(overrides: Partial<AppState> = {}): AppState {
  return {
    tab: 1,
    lang: "ru",
    selection: null,
    filters: { minCoauth: 1, minSharedAuthors: 1, yearMax: 2026 },
    ...overrides,
  };
}

describe("mountUrlSync", () => {
  beforeEach(() => {
    history.replaceState(null, "", "/");
  });

  it("при монтировании нормализует URL под текущее состояние store", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState({ tab: 2 }));

    mountUrlSync(store, data);

    expect(location.search).toBe("?tab=2");
  });

  it("смена selection в пределах той же вкладки — replaceState, история не растёт", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    mountUrlSync(store, data);
    const lengthBefore = history.length;

    const author = data.authors[0];
    if (!author) throw new Error("фикстура должна содержать хотя бы одного автора");
    store.set({ selection: { kind: "node", key: author.key } });

    expect(location.search).toBe(`?tab=1&sel=node&key=${author.key}`);
    expect(history.length).toBe(lengthBefore);
  });

  it("смена вкладки — pushState, история растёт на одну запись", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    mountUrlSync(store, data);
    const lengthBefore = history.length;

    store.set({ tab: 2 });

    expect(location.search).toBe("?tab=2");
    expect(history.length).toBe(lengthBefore + 1);
  });

  it("popstate возвращает состояние из URL в store и не создаёт новую запись истории", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    mountUrlSync(store, data);

    const author = data.authors[0];
    if (!author) throw new Error("фикстура должна содержать хотя бы одного автора");
    // Симулируем реальный порядок событий браузера: сначала меняется сам URL
    // (как при настоящем back/forward), потом приходит popstate.
    history.pushState(null, "", `?tab=3&sel=node&key=${author.key}`);
    const lengthBefore = history.length;

    window.dispatchEvent(new PopStateEvent("popstate"));

    expect(store.get().tab).toBe(3);
    expect(store.get().selection).toEqual({ kind: "node", key: author.key });
    expect(history.length).toBe(lengthBefore);
  });

  it("unmount снимает обработчик popstate", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    const unmount = mountUrlSync(store, data);
    unmount();

    history.pushState(null, "", "?tab=2");
    window.dispatchEvent(new PopStateEvent("popstate"));

    expect(store.get().tab).toBe(1);
  });
});
