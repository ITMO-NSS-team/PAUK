import { beforeEach, describe, expect, it, vi } from "vitest";
import type { PubDetail, RepoDetail } from "../src/contracts/graph";
import { mountGlobalSearch } from "../src/features/globalSearch";
import { Store, type AppState } from "../src/core/state";
import { loadSampleGraphData } from "./fixtures";

const NO_PUB_DETAILS = new Map<string, PubDetail>();
const NO_REPO_DETAILS = new Map<string, RepoDetail>();

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
      edgeZoomThreshold: 0.4,
      showRegions: { 1: false, 2: false, 3: false },
      regionZoomThreshold: 0.25,
      regionMinNodes: 10,
    },
    ...overrides,
  };
}

function mountMarkup(): void {
  document.body.innerHTML = `
    <button type="button" id="global-search-trigger"></button>
    <div id="global-search" hidden>
      <input type="search" id="global-search-input" />
      <div id="global-search-results"></div>
    </div>
  `;
}

describe("mountGlobalSearch", () => {
  beforeEach(() => {
    mountMarkup();
  });

  it("clicking the trigger button opens the dialog and moves focus to the input", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    mountGlobalSearch(store, data, NO_PUB_DETAILS, NO_REPO_DETAILS);

    document
      .getElementById("global-search-trigger")
      ?.dispatchEvent(new MouseEvent("click", { bubbles: true }));

    const overlay = document.getElementById("global-search") as HTMLElement;
    const input = document.getElementById("global-search-input") as HTMLInputElement;
    expect(overlay.hidden).toBe(false);
    expect(document.activeElement).toBe(input);
    expect(input.placeholder).toBe("Поиск по авторам, репозиториям, публикациям, департаментам…");
  });

  it("the '/' key opens the dialog when focus is not in a text field", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    mountGlobalSearch(store, data, NO_PUB_DETAILS, NO_REPO_DETAILS);

    document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "/", bubbles: true }));

    expect((document.getElementById("global-search") as HTMLElement).hidden).toBe(false);
  });

  it("the '/' key does NOT open the dialog when focus is in a text field, otherwise it would swallow ordinary character input", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    mountGlobalSearch(store, data, NO_PUB_DETAILS, NO_REPO_DETAILS);

    const decoyInput = document.createElement("input");
    decoyInput.type = "text";
    document.body.appendChild(decoyInput);
    decoyInput.focus();

    decoyInput.dispatchEvent(new KeyboardEvent("keydown", { key: "/", bubbles: true }));

    expect((document.getElementById("global-search") as HTMLElement).hidden).toBe(true);
  });

  it("Enter in the input selects the first result", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState({ tab: 2 })); // deliberately not the authors tab
    mountGlobalSearch(store, data, NO_PUB_DETAILS, NO_REPO_DETAILS);

    document
      .getElementById("global-search-trigger")
      ?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    const input = document.getElementById("global-search-input") as HTMLInputElement;
    input.value = "Иванов";
    input.dispatchEvent(new Event("input"));

    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));

    expect(store.get().tab).toBe(1);
    expect(store.get().selection).toEqual({ kind: "node", key: "A1" });
    expect((document.getElementById("global-search") as HTMLElement).hidden).toBe(true);
  });

  it("an external author is searchable only when the external authors filter is on", async () => {
    const sample = await loadSampleGraphData();
    const data = {
      ...sample,
      authors: sample.authors.map((a) => (a.key === "A1" ? { ...a, is_itmo: false } : a)),
    };
    const store = new Store<AppState>(initialState());
    mountGlobalSearch(store, data, NO_PUB_DETAILS, NO_REPO_DETAILS);
    const trigger = document.getElementById("global-search-trigger");
    const input = document.getElementById("global-search-input") as HTMLInputElement;
    const search = (query: string): string[] => {
      trigger?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      input.value = query;
      input.dispatchEvent(new Event("input"));
      return [...document.querySelectorAll("#global-search-results .tab-list-item")].map(
        (el) => el.textContent ?? "",
      );
    };

    expect(search("Иванов").some((text) => text.includes("Иванов"))).toBe(false);

    store.set({ filters: { ...store.get().filters, showExternalAuthors: true } });
    expect(search("Иванов").some((text) => text.includes("Иванов"))).toBe(true);
  });

  it("Enter pressed NOT in the input (e.g. already on a result button) does not replace the choice with the first result", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    mountGlobalSearch(store, data, NO_PUB_DETAILS, NO_REPO_DETAILS);

    document
      .getElementById("global-search-trigger")
      ?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    const input = document.getElementById("global-search-input") as HTMLInputElement;
    input.value = "П"; // matches more than one author
    input.dispatchEvent(new Event("input"));

    const items = [
      ...document.querySelectorAll<HTMLButtonElement>("#global-search-results .tab-list-item"),
    ];
    if (items.length < 2) throw new Error("this test needs at least two results");
    const second = items[1];
    if (!second) throw new Error("there must be a second result");

    // Focus is on the second result, not the input: Enter must click that
    // button, not pick the first result.
    second.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));

    expect(store.get().selection).toBeNull();
  });

  it("Escape closes the open dialog", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    mountGlobalSearch(store, data, NO_PUB_DETAILS, NO_REPO_DETAILS);

    document
      .getElementById("global-search-trigger")
      ?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));

    expect((document.getElementById("global-search") as HTMLElement).hidden).toBe(true);
  });

  it("clicking the dimmed backdrop (not the dialog itself) closes the search", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    mountGlobalSearch(store, data, NO_PUB_DETAILS, NO_REPO_DETAILS);

    const overlay = document.getElementById("global-search") as HTMLElement;
    document
      .getElementById("global-search-trigger")
      ?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    overlay.dispatchEvent(new MouseEvent("click", { bubbles: true })); // target is the overlay itself

    expect(overlay.hidden).toBe(true);
  });

  it("selecting an author from the results switches to tab 1 and writes selection", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState({ tab: 2 })); // deliberately not the authors tab
    mountGlobalSearch(store, data, NO_PUB_DETAILS, NO_REPO_DETAILS);

    document
      .getElementById("global-search-trigger")
      ?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    const input = document.getElementById("global-search-input") as HTMLInputElement;
    input.value = "Иванов";
    input.dispatchEvent(new Event("input"));

    const hit = document.querySelector<HTMLButtonElement>("#global-search-results .tab-list-item");
    if (!hit) throw new Error("at least one result must be found for 'Иванов'");
    hit.click();

    expect(store.get().screen).toBe("app");
    expect(store.get().tab).toBe(1);
    expect(store.get().selection).toEqual({ kind: "node", key: "A1" });
    expect((document.getElementById("global-search") as HTMLElement).hidden).toBe(true); // closed after the pick
  });

  it("the search dialog closes after selection, even if another Store subscriber failed on the new selection", async () => {
    // Regression: a panel crash on the selected node interrupted store.set(),
    // so close() never ran.
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    store.subscribe(() => {
      throw new Error("subscriber failed");
    });
    mountGlobalSearch(store, data, NO_PUB_DETAILS, NO_REPO_DETAILS);
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);

    document
      .getElementById("global-search-trigger")
      ?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    const input = document.getElementById("global-search-input") as HTMLInputElement;
    input.value = "Иванов";
    input.dispatchEvent(new Event("input"));
    document.querySelector<HTMLButtonElement>("#global-search-results .tab-list-item")?.click();

    expect(store.get().selection).toEqual({ kind: "node", key: "A1" });
    expect((document.getElementById("global-search") as HTMLElement).hidden).toBe(true);
    expect(consoleError).toHaveBeenCalled();
    consoleError.mockRestore();
  });

  it("selecting a department writes selection dept without touching tab", async () => {
    const data = await loadSampleGraphData();
    const dept = data.departments[0];
    if (!dept) throw new Error("the fixture must contain at least one department");
    const store = new Store<AppState>(initialState({ tab: 3 }));
    mountGlobalSearch(store, data, NO_PUB_DETAILS, NO_REPO_DETAILS);

    document
      .getElementById("global-search-trigger")
      ?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    const input = document.getElementById("global-search-input") as HTMLInputElement;
    input.value = dept.name;
    input.dispatchEvent(new Event("input"));

    const hit = [
      ...document.querySelectorAll<HTMLButtonElement>("#global-search-results .tab-list-item"),
    ].find((button) => button.dataset.kind === "dept");
    if (!hit) throw new Error(`department "${dept.name}" must be among the results`);
    hit.click();

    expect(store.get().tab).toBe(3); // unchanged
    expect(store.get().selection).toEqual({ kind: "dept", id: dept.id });
  });

  it("a query with no matches shows 'Ничего не найдено'", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    mountGlobalSearch(store, data, NO_PUB_DETAILS, NO_REPO_DETAILS);

    document
      .getElementById("global-search-trigger")
      ?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    const input = document.getElementById("global-search-input") as HTMLInputElement;
    input.value = "лщывалщыв"; // matches no fixture label
    input.dispatchEvent(new Event("input"));

    expect(document.querySelectorAll("#global-search-results .tab-list-item")).toHaveLength(0);
    expect(document.querySelector("#global-search-results .tab-empty")?.textContent).toBe(
      "Ничего не найдено",
    );
  });

  it("an empty query (right after opening) shows departments to browse, largest first, not 'Ничего не найдено' and not an empty list", async () => {
    const data = await loadSampleGraphData();
    // Department 0 (n=7) is larger than department 2 (n=5), so it comes first.
    const dept0 = data.departments.find((d) => d.id === 0);
    const dept1 = data.departments.find((d) => d.id === 2);
    if (!dept0 || !dept1) throw new Error("the fixture must contain departments 0 and 2");
    if (dept0.n <= dept1.n)
      throw new Error(
        "the fixture must give a spread of department sizes to check the sorting",
      );
    const store = new Store<AppState>(initialState());
    mountGlobalSearch(store, data, NO_PUB_DETAILS, NO_REPO_DETAILS);

    document
      .getElementById("global-search-trigger")
      ?.dispatchEvent(new MouseEvent("click", { bubbles: true }));

    expect(document.querySelector("#global-search-results .tab-empty")).toBeNull();
    expect(document.querySelector(".global-search-hint")?.textContent).toBe("Департаменты");
    const hits = [
      ...document.querySelectorAll<HTMLButtonElement>("#global-search-results .tab-list-item"),
    ];
    expect(hits.length).toBeGreaterThan(0);
    expect(hits.every((hit) => hit.dataset.kind === "dept")).toBe(true);
    expect(hits.findIndex((h) => h.textContent === dept0.name)).toBeLessThan(
      hits.findIndex((h) => h.textContent === dept1.name),
    );
  });

  it("clicking a department from the browse hint selects it", async () => {
    const data = await loadSampleGraphData();
    const dept = data.departments[0];
    if (!dept) throw new Error("the fixture must contain at least one department");
    const store = new Store<AppState>(initialState());
    mountGlobalSearch(store, data, NO_PUB_DETAILS, NO_REPO_DETAILS);

    document
      .getElementById("global-search-trigger")
      ?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    const hit = [
      ...document.querySelectorAll<HTMLButtonElement>("#global-search-results .tab-list-item"),
    ].find((button) => button.textContent === dept.name);
    if (!hit) throw new Error(`department "${dept.name}" must be in the browse hint`);

    hit.click();

    expect(store.get().selection).toEqual({ kind: "dept", id: dept.id });
    expect((document.getElementById("global-search") as HTMLElement).hidden).toBe(true);
  });
});
