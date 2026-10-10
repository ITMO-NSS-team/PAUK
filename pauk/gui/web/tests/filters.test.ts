import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FILTER_CONFIG } from "../src/core/config";
import type { GraphData } from "../src/contracts/graph";
import { mountFilters, mountHiddenAuthorReveal } from "../src/features/filters";
import { Store, type AppState } from "../src/core/state";

function initialState(overrides: Partial<AppState> = {}): AppState {
  return {
    screen: "app",
    tab: 1,
    lang: "ru",
    theme: "dark",
    selection: null,
    filters: {
      minCoauth: 1,
      minSharedAuthors: 1,
      yearMax: 2026,
      showNoDeptAuthors: true,
      showNoDeptPubs: true,
      showExternalAuthors: false,
      showIsolatedAuthors: true,
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

  it("labels the sidebar section (\"Фильтры\"/\"Filters\") for the current language", () => {
    withContainer(() => {
      const store = new Store<AppState>(initialState({ lang: "ru" }));
      mountFilters(store);
      expect(document.getElementById("filters-section-label")?.textContent).toBe("Фильтры");

      store.set({ lang: "en" });
      expect(document.getElementById("filters-section-label")?.textContent).toBe("Filters");
    });
  });

  it("on tab 1: edge zoom (shared), coauthorship threshold, then two shared region controls", () => {
    withContainer(() => {
      const store = new Store<AppState>(initialState());
      mountFilters(store);

      expect(container.hidden).toBe(false);
      const inputs = container.querySelectorAll("input[type='range']");
      expect(inputs).toHaveLength(4);
      expect((inputs[0] as HTMLInputElement).value).toBe("0.2"); // edge zoom comes first
      expect((inputs[1] as HTMLInputElement).value).toBe("1");
    });
  });

  it("on tab 3: edge zoom, shared authors, year and two region controls", () => {
    withContainer(() => {
      const store = new Store<AppState>(initialState({ tab: 3 }));
      mountFilters(store);

      expect(container.querySelectorAll("input[type='range']")).toHaveLength(5);
    });
  });

  it("on tab 2 (repositories) only the shared controls: edge zoom and regions, the panel is not hidden", () => {
    withContainer(() => {
      const store = new Store<AppState>(initialState({ tab: 2 }));
      mountFilters(store);

      expect(container.hidden).toBe(false);
      expect(container.querySelectorAll("input[type='range']")).toHaveLength(3);
      expect(container.querySelectorAll("input[type='checkbox']")).toHaveLength(1); // only the regions toggle
    });
  });

  it("moving a tab-specific slider writes the new value to store.filters with a delay (debounce), not instantly", () => {
    withContainer(() => {
      const store = new Store<AppState>(initialState());
      mountFilters(store);

      // [0] is the shared edge zoom slider.
      const input = container.querySelectorAll("input[type='range']")[1] as HTMLInputElement;
      input.value = "7";
      input.dispatchEvent(new Event("input"));

      // The value label updates at once, without waiting for the debounce.
      expect(container.querySelectorAll(".filter-row__value")[1]?.textContent).toBe("7");
      expect(store.get().filters.minCoauth).toBe(1); // not applied yet

      vi.advanceTimersByTime(FILTER_CONFIG.debounceMs - 1);
      expect(store.get().filters.minCoauth).toBe(1); // still not applied

      vi.advanceTimersByTime(1);
      expect(store.get().filters.minCoauth).toBe(7); // applied after debounceMs
    });
  });

  it("the thumb tilts in the direction of the slider movement", () => {
    withContainer(() => {
      const store = new Store<AppState>(initialState());
      mountFilters(store);

      const input = container.querySelectorAll("input[type='range']")[1] as HTMLInputElement;
      const move = (value: number): void => {
        input.value = String(value);
        input.dispatchEvent(new Event("input"));
      };

      move(5);
      expect(input.dataset.tilt).toBe("right");
      move(3);
      expect(input.dataset.tilt).toBe("left");

      input.dispatchEvent(new Event("pointerdown"));
      expect(input.dataset.tilt).toBeUndefined();
    });
  });

  it("fast slider dragging (many ticks in a row) applies ONLY the last value, not every tick", () => {
    withContainer(() => {
      const store = new Store<AppState>(initialState());
      mountFilters(store);

      const input = container.querySelectorAll("input[type='range']")[1] as HTMLInputElement;
      // Each input restarts the timer; only the value the user stopped on is applied.
      for (const value of [2, 3, 4, 5, 6, 7]) {
        input.value = String(value);
        input.dispatchEvent(new Event("input"));
        vi.advanceTimersByTime(FILTER_CONFIG.debounceMs - 1);
      }
      expect(store.get().filters.minCoauth).toBe(1); // no intermediate tick applied

      vi.advanceTimersByTime(1);
      expect(store.get().filters.minCoauth).toBe(7); // only the last value applied
    });
  });

  it("moving the shared edge zoom slider writes edgeZoomThreshold regardless of the tab", () => {
    withContainer(() => {
      const store = new Store<AppState>(initialState({ tab: 2 })); // a tab without its own filters
      mountFilters(store);

      const input = container.querySelector("input[type='range']") as HTMLInputElement;
      input.value = "0.1";
      input.dispatchEvent(new Event("input"));
      vi.advanceTimersByTime(FILTER_CONFIG.debounceMs);

      expect(store.get().filters.edgeZoomThreshold).toBe(0.1);
    });
  });

  it("switching tabs rebuilds the tab-specific controls, but the shared edge zoom stays", () => {
    withContainer(() => {
      const store = new Store<AppState>(initialState());
      mountFilters(store);

      store.set({ tab: 2 });

      expect(container.hidden).toBe(false);
      // Shared by all tabs: edge zoom, region zoom, region min nodes.
      expect(container.querySelectorAll("input[type='range']")).toHaveLength(3);
    });
  });

  it("tabs 1 and 3 have the \"Показывать без департамента\" checkbox, tab 2 does not", () => {
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

  it("unchecking the \"Показывать без департамента\" checkbox writes false to the matching filters field", () => {
    withContainer(() => {
      const store = new Store<AppState>(initialState());
      mountFilters(store);

      const checkbox = checkboxByLabel("Показывать без департамента");
      if (!checkbox) throw new Error("the \"Показывать без департамента\" checkbox must be on the authors tab");
      checkbox.checked = false;
      checkbox.dispatchEvent(new Event("change"));

      expect(store.get().filters.showNoDeptAuthors).toBe(false);
    });
  });
  it("the \"Показывать внешних авторов\" checkbox on the authors tab is off by default and enables the filter", () => {
    withContainer(() => {
      const store = new Store<AppState>(initialState());
      mountFilters(store);

      const checkbox = checkboxByLabel("Показывать внешних авторов");
      if (!checkbox) throw new Error("the external authors checkbox must be on the authors tab");
      expect(checkbox.checked).toBe(false);
      checkbox.checked = true;
      checkbox.dispatchEvent(new Event("change"));

      expect(store.get().filters.showExternalAuthors).toBe(true);
    });
  });

  it("the \"Регионы департаментов\" checkbox is on every tab and toggles only the current one", () => {
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
      if (!checkbox) throw new Error("the regions checkbox must be on the publications tab");
      checkbox.checked = false;
      checkbox.dispatchEvent(new Event("change"));
      expect(store.get().filters.showRegions).toEqual({ 1: true, 2: false, 3: false });

      store.set({ tab: 2 });
      expect(checkboxByLabel("Регионы департаментов")?.checked).toBe(false);
    });
  });

  it("region sliders write regionZoomThreshold and regionMinNodes, the edge zoom range does not overlap the region range", () => {
    withContainer(() => {
      const store = new Store<AppState>(initialState());
      mountFilters(store);

      const ranges = [...container.querySelectorAll<HTMLInputElement>("input[type='range']")];
      const [edgeZoom] = ranges;
      const [regionZoom, minNodes] = ranges.slice(-2); // region rows come last
      expect(Number(edgeZoom?.max)).toBeLessThanOrEqual(Number(regionZoom?.min));

      if (!regionZoom || !minNodes) throw new Error("the region sliders must be in the filters");
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

describe("mountHiddenAuthorReveal", () => {
  // A1, A2: ITMO co-authors of P1; A3: alone on P2, no repos (isolated); E1: external.
  const data = {
    authors: [
      { key: "A1", is_itmo: true, pubs_count: 1 },
      { key: "A2", is_itmo: true, pubs_count: 1 },
      { key: "A3", is_itmo: true, pubs_count: 1 },
      { key: "E1", is_itmo: false, pubs_count: 1 },
    ],
    all_edges: [
      { s: "A1", t: "P1" },
      { s: "A2", t: "P1" },
      { s: "E1", t: "P1" },
      { s: "A3", t: "P2" },
    ],
    repo_author_edges: [],
  } as unknown as GraphData;
  const hiddenByDefault = () =>
    initialState({
      filters: {
        ...initialState().filters,
        showExternalAuthors: false,
        showIsolatedAuthors: false,
      },
    });

  it("selecting a hidden external author enables the external filter, otherwise the selection would go nowhere", () => {
    const store = new Store<AppState>(hiddenByDefault());
    mountHiddenAuthorReveal(store, data);

    store.set({ selection: { kind: "node", key: "E1" } });

    expect(store.get().filters.showExternalAuthors).toBe(true);
    expect(store.get().filters.showIsolatedAuthors).toBe(false);
    expect(store.get().selection).toEqual({ kind: "node", key: "E1" });
  });

  it("selecting an author without links enables the authors-without-links filter", () => {
    const store = new Store<AppState>(hiddenByDefault());
    mountHiddenAuthorReveal(store, data);

    store.set({ selection: { kind: "node", key: "A3" } });

    expect(store.get().filters.showIsolatedAuthors).toBe(true);
    expect(store.get().filters.showExternalAuthors).toBe(false);
  });

  it("selecting an external coauthor of an author without links enables only the external filter, which gives the author a link", () => {
    const soloWithExternal = {
      authors: [
        { key: "A1", is_itmo: true, pubs_count: 1 },
        { key: "E1", is_itmo: false, pubs_count: 1 },
      ],
      all_edges: [
        { s: "A1", t: "P1" },
        { s: "E1", t: "P1" },
      ],
      repo_author_edges: [],
    } as unknown as GraphData;
    const store = new Store<AppState>(hiddenByDefault());
    mountHiddenAuthorReveal(store, soloWithExternal);

    store.set({ selection: { kind: "node", key: "E1" } });

    expect(store.get().filters.showExternalAuthors).toBe(true);
    expect(store.get().filters.showIsolatedAuthors).toBe(false);
  });

  it("selecting a regular ITMO author leaves the filters untouched", () => {
    const store = new Store<AppState>(hiddenByDefault());
    mountHiddenAuthorReveal(store, data);

    store.set({ selection: { kind: "node", key: "A1" } });

    expect(store.get().filters.showExternalAuthors).toBe(false);
    expect(store.get().filters.showIsolatedAuthors).toBe(false);
  });
});
