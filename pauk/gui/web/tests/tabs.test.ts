import type Sigma from "sigma";
import { describe, expect, it } from "vitest";
import type { GraphData, PubDetail, RepoDetail } from "../src/contracts/graph";
import { indexDetailsByKey } from "../src/core/data";
import { Store, type AppState } from "../src/core/state";
import { mountTabs } from "../src/features/tabs";
import { authorsTab } from "../src/features/tabs/authors";
import { pubsTab } from "../src/features/tabs/pubs";
import { reposTab } from "../src/features/tabs/repos";
import { loadSampleGraphData, loadSamplePubDetails } from "./fixtures";

const NO_PUB_DETAILS = new Map<string, PubDetail>();
const NO_REPO_DETAILS = new Map<string, RepoDetail>();

/** List tabs do not use the renderer; the camera is moved by map/build.ts. */
function fakeRenderer(): Sigma {
  return {} as unknown as Sigma;
}

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

/** Rows are the second child; the first is the search input. */
function listItems(container: HTMLElement): HTMLButtonElement[] {
  return Array.from(container.querySelectorAll<HTMLButtonElement>(".tab-list-item"));
}

/**
 * More authors than one page. `pubs_count` falls with the index, so the
 * sorted order equals the index order.
 */
function manyAuthorsData(count: number): GraphData {
  return {
    departments: [],
    dept_edges: [],
    authors: Array.from({ length: count }, (_, i) => ({
      key: `A${i}`,
      kind: "author" as const,
      dept: 0,
      label: `Автор ${String(i).padStart(2, "0")}`,
      label_en: `Author ${String(i).padStart(2, "0")}`,
      pubs_count: count - i,
      rank: 1,
      gx: i,
      gy: i,
    })),
    coauth_edges: [],
    repos: [],
    repo_edges: [],
    repo_author_edges: [],
    repo_pub_edges: [],
    pubs: [],
    pub_edges: [],
    all_edges: [],
  };
}

describe("authorsTab", () => {
  it("renders authors by descending pubs_count and highlights the selected one", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    const container = document.createElement("div");

    authorsTab.mount(container, store, fakeRenderer(), data, NO_PUB_DETAILS, NO_REPO_DETAILS);

    const sorted = [...data.authors].sort((a, b) => b.pubs_count - a.pubs_count);
    expect(listItems(container).map((el) => el.textContent)).toEqual(
      sorted.map((a) => `${a.label}${a.pubs_count}`),
    );

    const first = sorted[0];
    if (!first) throw new Error("the fixture must contain at least one author");
    store.set({ selection: { kind: "node", key: first.key } });

    expect(listItems(container)[0]?.classList.contains("tab-list-item--selected")).toBe(true);
  });

  it("external authors appear in the list only with the filter on", async () => {
    const sample = await loadSampleGraphData();
    const [first, ...rest] = [...sample.authors].sort((a, b) => b.pubs_count - a.pubs_count);
    if (!first) throw new Error("the fixture must contain at least one author");
    const data = { ...sample, authors: [{ ...first, is_itmo: false }, ...rest] };
    const store = new Store<AppState>(initialState());
    const container = document.createElement("div");

    authorsTab.mount(container, store, fakeRenderer(), data, NO_PUB_DETAILS, NO_REPO_DETAILS);
    expect(listItems(container).map((el) => el.textContent)).not.toContain(
      `${first.label}${first.pubs_count}`,
    );

    store.set({ filters: { ...store.get().filters, showExternalAuthors: true } });
    expect(listItems(container)[0]?.textContent).toBe(`${first.label}${first.pubs_count}`);
  });

  it("switching lang to en shows label_en instead of label", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    const container = document.createElement("div");

    authorsTab.mount(container, store, fakeRenderer(), data, NO_PUB_DETAILS, NO_REPO_DETAILS);
    store.set({ lang: "en" });

    const sorted = [...data.authors].sort((a, b) => b.pubs_count - a.pubs_count);
    expect(listItems(container).map((el) => el.textContent)).toEqual(
      sorted.map((a) => `${a.label_en}${a.pubs_count}`),
    );
  });

  it("clicking an author writes the selection to the store, the camera flight is done by map/build.ts, not the tab", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    const container = document.createElement("div");

    authorsTab.mount(container, store, fakeRenderer(), data, NO_PUB_DETAILS, NO_REPO_DETAILS);
    listItems(container)[0]?.click();

    const author = [...data.authors].sort((a, b) => b.pubs_count - a.pubs_count)[0];
    expect(store.get().selection).toEqual({ kind: "node", key: author?.key });
  });

  it("the search field filters the list by substring of the label, case-insensitively", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    const container = document.createElement("div");

    authorsTab.mount(container, store, fakeRenderer(), data, NO_PUB_DETAILS, NO_REPO_DETAILS);
    const search = container.querySelector<HTMLInputElement>(".tab-search");
    if (!search) throw new Error("the tab must contain a search field");

    search.value = "иванов";
    search.dispatchEvent(new Event("input"));

    expect(listItems(container).map((el) => el.textContent)).toEqual(["Иванов И.И.4"]);
  });

  it("an empty search query shows the whole list again", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    const container = document.createElement("div");

    authorsTab.mount(container, store, fakeRenderer(), data, NO_PUB_DETAILS, NO_REPO_DETAILS);
    const search = container.querySelector<HTMLInputElement>(".tab-search");
    if (!search) throw new Error("the tab must contain a search field");

    search.value = "иванов";
    search.dispatchEvent(new Event("input"));
    search.value = "";
    search.dispatchEvent(new Event("input"));

    expect(listItems(container)).toHaveLength(data.authors.length);
  });

  it("a query with no matches shows 'Ничего не найдено' instead of an empty list", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    const container = document.createElement("div");

    authorsTab.mount(container, store, fakeRenderer(), data, NO_PUB_DETAILS, NO_REPO_DETAILS);
    const search = container.querySelector<HTMLInputElement>(".tab-search");
    if (!search) throw new Error("the tab must contain a search field");

    search.value = "лщывалщыв"; // matches no fixture label
    search.dispatchEvent(new Event("input"));

    expect(listItems(container)).toHaveLength(0);
    expect(container.querySelector(".tab-empty")?.textContent).toBe("Ничего не найдено");
  });
});

describe("reposTab", () => {
  it("sorts repositories by stars in descending order", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    const container = document.createElement("div");

    reposTab.mount(container, store, fakeRenderer(), data, NO_PUB_DETAILS, NO_REPO_DETAILS);

    const stars = listItems(container).map((el) => Number(el.textContent?.match(/\d+/)?.[0]));
    expect(stars).toEqual([...stars].sort((a, b) => b - a));
  });
});

describe("mountTabs tab switching", () => {
  function buttonsMarkup(): HTMLElement {
    const nav = document.createElement("nav");
    nav.innerHTML = `
      <button type="button" data-tab="1">Авторы</button>
      <button type="button" data-tab="2">Репозитории</button>
      <button type="button" data-tab="3">Публикации</button>
    `;
    return nav;
  }

  it("by default mounts tab 1 (authors) and highlights its button", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    const buttons = buttonsMarkup();
    const content = document.createElement("div");

    mountTabs(buttons, content, store, fakeRenderer(), data, NO_PUB_DETAILS, NO_REPO_DETAILS);

    expect(listItems(content)).toHaveLength(data.authors.length);
    expect(buttons.querySelector('[data-tab="1"]')?.classList.contains("tab-button--active")).toBe(
      true,
    );
  });

  it("clicking a tab button unmounts the old tab and mounts the new one", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    const buttons = buttonsMarkup();
    const content = document.createElement("div");

    mountTabs(buttons, content, store, fakeRenderer(), data, NO_PUB_DETAILS, NO_REPO_DETAILS);
    (buttons.querySelector('[data-tab="2"]') as HTMLButtonElement).click();

    expect(store.get().tab).toBe(2);
    expect(listItems(content)).toHaveLength(data.repos.length);
    expect(buttons.querySelector('[data-tab="2"]')?.classList.contains("tab-button--active")).toBe(
      true,
    );
    expect(buttons.querySelector('[data-tab="1"]')?.classList.contains("tab-button--active")).toBe(
      false,
    );
  });

  it("clicking a tab button does not touch selection by itself, resetting a stale selection on graph rebuild is done by map/build.ts::mountReactiveGraph (see tests/build.test.ts)", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState({ selection: { kind: "node", key: "A1" } }));
    const buttons = buttonsMarkup();
    const content = document.createElement("div");

    mountTabs(buttons, content, store, fakeRenderer(), data, NO_PUB_DETAILS, NO_REPO_DETAILS);
    (buttons.querySelector('[data-tab="2"]') as HTMLButtonElement).click();

    expect(store.get().selection).toEqual({ kind: "node", key: "A1" });
  });
});

describe("pubsTab", () => {
  it("publications without a year (year === null) go to the end of the list", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    const container = document.createElement("div");

    pubsTab.mount(container, store, fakeRenderer(), data, NO_PUB_DETAILS, NO_REPO_DETAILS);

    const years = listItems(container).map((el) => el.textContent?.includes("неизвестен"));
    // After the first unknown year, all the rest are unknown too.
    const firstUnknownIndex = years.indexOf(true);
    if (firstUnknownIndex !== -1) {
      expect(years.slice(firstUnknownIndex).every(Boolean)).toBe(true);
    }
  });

  it("shows the real publication title from pubDetails instead of the key", async () => {
    const data = await loadSampleGraphData();
    const pubDetails = indexDetailsByKey(await loadSamplePubDetails());
    const store = new Store<AppState>(initialState());
    const container = document.createElement("div");

    pubsTab.mount(container, store, fakeRenderer(), data, pubDetails, NO_REPO_DETAILS);

    const labels = Array.from(container.querySelectorAll(".tab-list-item__label")).map(
      (el) => el.textContent,
    );
    for (const pub of data.pubs) {
      expect(labels).toContain(pubDetails.get(pub.key)?.label);
    }
  });
});

describe("createNodeListTab paged list (TAB_LIST_CONFIG.pageSize)", () => {
  it("shows the section label (\"Быстрый поиск\"/\"Quick Search\") for the current language", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState({ lang: "ru" }));
    const container = document.createElement("div");

    authorsTab.mount(container, store, fakeRenderer(), data, NO_PUB_DETAILS, NO_REPO_DETAILS);
    expect(container.querySelector(".sidebar-section-label")?.textContent).toBe("Быстрый поиск");

    store.set({ lang: "en" });
    expect(container.querySelector(".sidebar-section-label")?.textContent).toBe("Quick Search");
  });

  it("cuts the list to TAB_LIST_CONFIG.pageSize rows at a time and pages with '‹'/'›'", () => {
    const data = manyAuthorsData(25);
    const store = new Store<AppState>(initialState());
    const container = document.createElement("div");

    authorsTab.mount(container, store, fakeRenderer(), data, NO_PUB_DETAILS, NO_REPO_DETAILS);

    expect(listItems(container)).toHaveLength(10);
    expect(listItems(container)[0]?.textContent).toContain("Автор 00");
    expect(container.querySelector(".tab-pagination__status")?.textContent).toBe("1 / 3"); // 25 / 10 = 3 pages

    const next = container.querySelector<HTMLButtonElement>(
      '.tab-pagination__button[aria-label="Следующая страница"]',
    );
    if (!next) throw new Error("there must be a 'next page' button");
    next.click();

    expect(listItems(container)).toHaveLength(10);
    expect(listItems(container)[0]?.textContent).toContain("Автор 10");
    expect(container.querySelector(".tab-pagination__status")?.textContent).toBe("2 / 3");

    const prev = container.querySelector<HTMLButtonElement>(
      '.tab-pagination__button[aria-label="Предыдущая страница"]',
    );
    if (!prev) throw new Error("there must be a 'previous page' button");
    prev.click();

    expect(listItems(container)[0]?.textContent).toContain("Автор 00");
    expect(container.querySelector(".tab-pagination__status")?.textContent).toBe("1 / 3");
  });

  it("a new search query resets the page to the first", () => {
    const data = manyAuthorsData(25);
    const store = new Store<AppState>(initialState());
    const container = document.createElement("div");

    authorsTab.mount(container, store, fakeRenderer(), data, NO_PUB_DETAILS, NO_REPO_DETAILS);
    container
      .querySelector<HTMLButtonElement>('.tab-pagination__button[aria-label="Следующая страница"]')
      ?.click();
    expect(container.querySelector(".tab-pagination__status")?.textContent).toBe("2 / 3");

    const search = container.querySelector<HTMLInputElement>(".tab-search");
    if (!search) throw new Error("the tab must contain a search field");
    // Exactly "Автор 10".."Автор 19": one full page.
    search.value = "Автор 1";
    search.dispatchEvent(new Event("input"));

    expect(listItems(container)).toHaveLength(10);
    // One page: no pagination controls at all.
    expect(container.querySelector(".tab-pagination")?.children).toHaveLength(0);
  });

  it("a short list (less than pageSize) shows no pagination at all", async () => {
    const data = await loadSampleGraphData(); // 8 authors, fewer than pageSize
    const store = new Store<AppState>(initialState());
    const container = document.createElement("div");

    authorsTab.mount(container, store, fakeRenderer(), data, NO_PUB_DETAILS, NO_REPO_DETAILS);

    expect(container.querySelector(".tab-pagination")?.children).toHaveLength(0);
  });
});
