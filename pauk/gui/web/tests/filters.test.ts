import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FILTER_CONFIG } from "../src/core/config";
import type { GraphData } from "../src/contracts/graph";
import { t } from "../src/core/i18n";
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

  it("synchronizes programmatic checkboxes and sliders without replacing their DOM", () => {
    withContainer(() => {
      const store = new Store<AppState>(initialState());
      const unmount = mountFilters(store);
      const controls = [...container.querySelectorAll("input")];
      store.set({ filters: {
        ...store.get().filters,
        minCoauth: 8,
        showNoDeptAuthors: false,
        showExternalAuthors: true,
        showIsolatedAuthors: false,
        showRegions: { 1: true, 2: false, 3: false },
      } });

      expect([...container.querySelectorAll("input")]).toEqual(controls);
      expect(checkboxByLabel("Показывать без департамента")?.checked).toBe(false);
      expect(checkboxByLabel("Показывать внешних авторов")?.checked).toBe(true);
      expect(checkboxByLabel(t("filter.showIsolated", "ru"))?.checked).toBe(false);
      expect(checkboxByLabel("Регионы департаментов")?.checked).toBe(true);
      expect(container.querySelectorAll<HTMLInputElement>("input[type='range']")[1]?.value).toBe("8");
      expect(container.querySelectorAll(".filter-row__value")[1]?.textContent).toBe("8");
      unmount();
    });
  });

  it("preserves focused slider DOM, drag value and pending debounce across notifications and other filter changes", () => {
    withContainer(() => {
      const store = new Store<AppState>(initialState());
      const unmount = mountFilters(store);
      const inputs = [...container.querySelectorAll<HTMLInputElement>("input[type='range']")];
      const input = inputs[1];
      if (!input) throw new Error("the coauthorship slider must be present");
      input.focus();
      input.value = "7";
      input.dispatchEvent(new Event("input"));
      vi.advanceTimersByTime(100);
      store.notify();
      store.set({ theme: "light", selection: { kind: "node", key: "A1" } });
      store.set({ filters: { ...store.get().filters, regionMinNodes: 25, showExternalAuthors: true } });

      expect([...container.querySelectorAll("input[type='range']")]).toEqual(inputs);
      expect(document.activeElement).toBe(input);
      expect(input.value).toBe("7");
      expect(input.dataset.tilt).toBe("right");
      expect(container.querySelectorAll(".filter-row__value")[1]?.textContent).toBe("7");
      expect(inputs[3]?.value).toBe("25");
      expect(container.querySelectorAll(".filter-row__value")[3]?.textContent).toBe("25");
      expect(checkboxByLabel("Показывать внешних авторов")?.checked).toBe(true);
      vi.advanceTimersByTime(FILTER_CONFIG.debounceMs - 101);
      expect(store.get().filters.minCoauth).toBe(1);
      vi.advanceTimersByTime(1);
      expect(store.get().filters.minCoauth).toBe(7);
      unmount();
    });
  });

  it("lets an external update of the pending field win over the old debounce", () => {
    withContainer(() => {
      const store = new Store<AppState>(initialState());
      const unmount = mountFilters(store);
      const input = container.querySelectorAll<HTMLInputElement>("input[type='range']")[1];
      if (!input) throw new Error("the coauthorship slider must be present");
      input.focus();
      input.value = "7";
      input.dispatchEvent(new Event("input"));
      vi.advanceTimersByTime(100);
      store.set({ filters: { ...store.get().filters, minCoauth: 3 } });
      expect(document.activeElement).toBe(input);
      expect(input.value).toBe("3");
      expect(container.querySelectorAll(".filter-row__value")[1]?.textContent).toBe("3");
      vi.advanceTimersByTime(FILTER_CONFIG.debounceMs);
      expect(store.get().filters.minCoauth).toBe(3);
      expect(vi.getTimerCount()).toBe(0);
      unmount();
    });
  });

  it.each([{ tab: 3 as const }, { lang: "en" as const }])(
    "cancels shared and tab-specific pending sliders when rebuilding for %j",
    (patch) => {
      withContainer(() => {
        const store = new Store<AppState>(initialState());
        const unmount = mountFilters(store);
        const inputs = container.querySelectorAll<HTMLInputElement>("input[type='range']");
        const edge = inputs[0];
        const coauth = inputs[1];
        if (!edge || !coauth) throw new Error("the initial sliders must be present");
        edge.value = "0.1";
        coauth.value = "7";
        edge.dispatchEvent(new Event("input"));
        coauth.dispatchEvent(new Event("input"));
        expect(vi.getTimerCount()).toBe(2);
        store.set(patch);
        expect(container.querySelector("input[type='range']")).not.toBe(edge);
        expect(vi.getTimerCount()).toBe(0);
        edge.dispatchEvent(new Event("input"));
        coauth.dispatchEvent(new Event("input"));
        vi.advanceTimersByTime(FILTER_CONFIG.debounceMs);
        expect(store.get().filters).toMatchObject({ edgeZoomThreshold: 0.2, minCoauth: 1 });
        unmount();
      });
    },
  );

  it("cancels slider timers and subscriptions on unmount", () => {
    withContainer(() => {
      const store = new Store<AppState>(initialState());
      const unmount = mountFilters(store);
      const input = container.querySelector<HTMLInputElement>("input[type='range']");
      if (!input) throw new Error("the shared slider must be present");
      input.value = "0.1";
      input.dispatchEvent(new Event("input"));
      unmount();
      expect(vi.getTimerCount()).toBe(0);
      store.set({ tab: 3, lang: "en" });
      expect(container.querySelector("input[type='range']")).toBe(input);
      input.dispatchEvent(new Event("input"));
      vi.advanceTimersByTime(FILTER_CONFIG.debounceMs);
      expect(store.get().filters.edgeZoomThreshold).toBe(0.2);
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

  it.each([
    ["E1", "showExternalAuthors"],
    ["A3", "showIsolatedAuthors"],
  ] as const)("does not undo an explicit filter change for selected %s", (key, field) => {
    const store = new Store<AppState>(hiddenByDefault());
    const unmount = mountHiddenAuthorReveal(store, data);
    store.set({ selection: { kind: "node", key } });
    expect(store.get().filters[field]).toBe(true);
    store.set({ filters: { ...store.get().filters, [field]: false } });
    store.notify();
    store.set({ lang: "en", theme: "light" });
    store.set({ filters: { ...store.get().filters, minCoauth: 2 } });
    expect(store.get().filters[field]).toBe(false);

    store.set({ selection: { kind: "node", key } });
    expect(store.get().filters[field]).toBe(true);
    unmount();
  });

  it("reveals on a screen/tab navigation transition even if the node selection is retained", () => {
    const selection = { kind: "node", key: "E1" } as const;
    const store = new Store<AppState>(hiddenByDefault());
    mountHiddenAuthorReveal(store, data);
    store.set({ screen: "menu", selection });
    store.set({ filters: { ...store.get().filters, showExternalAuthors: false } });
    store.set({ screen: "app" });
    expect(store.get().filters.showExternalAuthors).toBe(true);
    store.set({ filters: { ...store.get().filters, showExternalAuthors: false } });
    store.set({ tab: 2 });
    store.set({ tab: 1 });
    expect(store.get().filters.showExternalAuthors).toBe(true);
  });

  it("never reveals hidden authors for edges", () => {
    const store = new Store<AppState>(hiddenByDefault());
    mountHiddenAuthorReveal(store, data);
    store.set({ selection: { kind: "edge", s: "A1", t: "E1", w: 1 } });
    store.notify();
    expect(store.get().filters).toMatchObject({ showExternalAuthors: false, showIsolatedAuthors: false });
  });

  it("selecting a regular ITMO author leaves the filters untouched", () => {
    const store = new Store<AppState>(hiddenByDefault());
    mountHiddenAuthorReveal(store, data);

    store.set({ selection: { kind: "node", key: "A1" } });

    expect(store.get().filters.showExternalAuthors).toBe(false);
    expect(store.get().filters.showIsolatedAuthors).toBe(false);
  });
});
