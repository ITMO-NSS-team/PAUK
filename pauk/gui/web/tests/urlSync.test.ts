import { beforeEach, describe, expect, it } from "vitest";
import { Store, type AppState } from "../src/core/state";
import { mountUrlSync } from "../src/features/urlSync";
import { loadSampleGraphData } from "./fixtures";

function initialState(overrides: Partial<AppState> = {}): AppState {
  return {
    screen: "app",
    tab: 1,
    lang: "ru",
    theme: "dark",
    selection: null,
    filters: { minCoauth: 1, minSharedAuthors: 1, yearMax: 2026, showNoDeptAuthors: true, showNoDeptPubs: true, showExternalAuthors: false, showIsolatedAuthors: true, edgeZoomThreshold: 0.4, showRegions: { 1: false, 2: false, 3: false }, regionZoomThreshold: 0.25, regionMinNodes: 10 },
    ...overrides,
  };
}

describe("mountUrlSync", () => {
  beforeEach(() => {
    history.replaceState(null, "", "/");
  });

  it("on a clean URL on mount unconditionally writes the current store state", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState({ tab: 2 }));

    mountUrlSync(store, data);

    expect(location.search).toBe("?tab=repos");
  });

  it("screen: 'menu' on mount writes tab=menu, not an empty query", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState({ screen: "menu" }));

    mountUrlSync(store, data);

    expect(location.search).toBe("?tab=menu");
  });

  it("on a non-empty (partial/broken) query normalizes the URL to the current store state", async () => {
    const data = await loadSampleGraphData();
    history.replaceState(null, "", "?tab=repos&sel=bogus");
    const store = new Store<AppState>(initialState({ tab: 2 }));

    mountUrlSync(store, data);

    expect(location.search).toBe("?tab=repos");
  });

  it("changing selection within the same tab uses replaceState, history does not grow", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    mountUrlSync(store, data);
    const lengthBefore = history.length;

    const author = data.authors[0];
    if (!author) throw new Error("the fixture must contain at least one author");
    store.set({ selection: { kind: "node", key: author.key } });

    expect(location.search).toBe(`?tab=persons&sel=node&key=${author.key}`);
    expect(history.length).toBe(lengthBefore);
  });

  it("changing the tab uses pushState, history grows by one entry", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    mountUrlSync(store, data);
    const lengthBefore = history.length;

    store.set({ tab: 2 });

    expect(location.search).toBe("?tab=repos");
    expect(history.length).toBe(lengthBefore + 1);
  });

  it("changing screen (menu -> app) uses pushState, like a tab change", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState({ screen: "menu" }));
    mountUrlSync(store, data);
    const lengthBefore = history.length;

    store.set({ screen: "app", tab: 2 });

    expect(location.search).toBe("?tab=repos");
    expect(history.length).toBe(lengthBefore + 1);
  });

  it("popstate restores the state from the URL into the store and does not create a new history entry", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    mountUrlSync(store, data);

    const pub = data.pubs[0];
    if (!pub) throw new Error("the fixture must contain at least one publication");
    // Browser order: the URL changes first, then popstate fires. A pub on
    // tab=pubs, since the node must belong to its tab.
    history.pushState(null, "", `?tab=pubs&sel=node&key=${pub.key}`);
    const lengthBefore = history.length;

    window.dispatchEvent(new PopStateEvent("popstate"));

    expect(store.get().tab).toBe(3);
    expect(store.get().selection).toEqual({ kind: "node", key: pub.key });
    expect(history.length).toBe(lengthBefore);
  });

  it("returns to the selected author, filters and camera position", async () => {
    const data = await loadSampleGraphData();
    const author = data.authors[0];
    if (!author) throw new Error("missing author fixture");
    const store = new Store<AppState>(initialState({ selection: { kind: "node", key: author.key } }));
    const initial = store.get();
    const position = { x: 0.2, y: 0.6, ratio: 0.3, angle: 0 };
    let current = position;
    const camera = {
      getState: () => current,
      async animate(state: typeof position) { current = state; },
    };
    const button = document.createElement("button");
    const unmount = mountUrlSync(store, data, { camera, button });
    try {
      expect(button.hidden).toBe(true);
      store.set({ tab: 2, selection: null });
      expect(button.textContent).toContain("Авторы");
      current = { ...position, x: 0.9 };
      store.set({ filters: { ...initial.filters, minCoauth: 3 } });
      const popped = new Promise<void>((done) => window.addEventListener("popstate", () => done(), { once: true }));
      button.click();
      await popped;
      expect(store.get()).toEqual(initial);
      expect(current).toEqual(position);
      expect(button.hidden).toBe(true);
    } finally {
      unmount();
    }
  });

  it("unmount removes the popstate handler", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    const unmount = mountUrlSync(store, data);
    unmount();

    history.pushState(null, "", "?tab=repos");
    window.dispatchEvent(new PopStateEvent("popstate"));

    expect(store.get().tab).toBe(1);
  });
});
