import type Graph from "graphology";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import type { GraphData } from "../src/contracts/graph";
import type { AppState, Store } from "../src/core/state";
import { FILTER_CONFIG } from "../src/core/config";
import { t } from "../src/core/i18n";
import {
  loadSampleAuthorDetails,
  loadSampleGraphData,
  loadSamplePubDetails,
  loadSampleRepoDetails,
} from "./fixtures";

const captured = vi.hoisted(() => ({
  store: null as Store<AppState> | null,
  renderers: [] as FakeSigma[],
}));

class FakeEmitter {
  private readonly listeners = new Map<string, Set<(payload: unknown) => void>>();

  on(type: string, listener: (payload: unknown) => void): void {
    const listeners = this.listeners.get(type) ?? new Set();
    listeners.add(listener);
    this.listeners.set(type, listeners);
  }

  off(type: string, listener: (payload: unknown) => void): void {
    this.listeners.get(type)?.delete(listener);
  }

  emit(type: string, payload: unknown): void {
    this.listeners.get(type)?.forEach((listener) => listener(payload));
  }

  removeAllListeners(): void {
    this.listeners.clear();
  }
}

class FakeSigma extends FakeEmitter {
  readonly settings = new Map<string, unknown>();
  readonly refresh = vi.fn();
  readonly camera = Object.assign(new FakeEmitter(), {
    getState: () => ({ x: 0.5, y: 0.5, ratio: 1 }),
    animate: vi.fn(),
  });

  constructor(
    readonly graph: Graph,
    private readonly container: HTMLElement,
  ) {
    super();
    captured.renderers.push(this);
  }

  getGraph(): Graph {
    return this.graph;
  }

  getContainer(): HTMLElement {
    return this.container;
  }

  getCamera(): typeof this.camera {
    return this.camera;
  }

  getNodeDisplayData(key: string): { x: number; y: number } | undefined {
    if (!this.graph.hasNode(key)) return undefined;
    return {
      x: this.graph.getNodeAttribute(key, "x") as number,
      y: this.graph.getNodeAttribute(key, "y") as number,
    };
  }

  setSetting(name: string, value: unknown): void {
    this.settings.set(name, value);
  }
}

vi.mock("sigma", () => ({ default: FakeSigma }));
vi.mock("../src/map/regions", () => ({
  mountRegions: () => ({ deptAtViewport: () => null }),
}));
vi.mock("../src/features/start", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/features/start")>();
  return {
    ...actual,
    mountStart: (store: Store<AppState>) => {
      captured.store = store;
      return actual.mountStart(store);
    },
  };
});

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function response(value: unknown, status = 200): Response {
  return { ok: status === 200, status, json: async () => value } as Response;
}

function element(id: string): HTMLElement {
  const found = document.getElementById(id);
  if (!found) throw new Error(`Missing test element #${id}`);
  return found;
}

function store(): Store<AppState> {
  if (!captured.store) throw new Error("The entrypoint has not created its store");
  return captured.store;
}

function renderer(): FakeSigma {
  const found = captured.renderers[0];
  if (!found) throw new Error("The entrypoint has not created its renderer");
  return found;
}

async function flush(): Promise<void> {
  // Fetch, response.json(), loggedStep and the entrypoint each add a microtask.
  for (let i = 0; i < 12; i += 1) await Promise.resolve();
}

async function graphFixture(): Promise<GraphData> {
  const sample = await loadSampleGraphData();
  return {
    ...sample,
    authors: [
      ...sample.authors,
      {
        key: "AE",
        kind: "author",
        is_itmo: false,
        dept: 0,
        label: "External",
        label_en: "External",
        pubs_count: 1,
        rank: 9,
        gx: 0.7,
        gy: 0.2,
      },
      {
        key: "AI",
        kind: "author",
        dept: 0,
        label: "Isolated",
        label_en: "Isolated",
        pubs_count: 0,
        rank: 10,
        gx: 0.8,
        gy: 0.2,
      },
      {
        key: "AX",
        kind: "author",
        is_itmo: false,
        dept: 0,
        label: "Both hidden",
        label_en: "Both hidden",
        pubs_count: 0,
        rank: 11,
        gx: 0.9,
        gy: 0.2,
      },
    ],
    all_edges: [...sample.all_edges, { s: "AE", t: "P1" }],
    coauth_edges: [...sample.coauth_edges, { s: "A1", t: "AE", w: 1 }],
    repos: sample.repos.map((repo) => (repo.key === "R1" ? { ...repo, group: 9 } : repo)),
    repo_groups: [
      {
        id: 9,
        kind: "org",
        name: "Synthetic org",
        name_en: "Synthetic org",
        color: "#3555e0",
        n: 1,
        n_authors: 0,
        n_pubs: 0,
        n_repos: 1,
      },
    ],
    pub_edges: sample.pub_edges.map((edge) =>
      edge.s === "P1" && edge.t === "P2" ? { ...edge, w: 4 } : edge,
    ),
    version_edges: [
      { s: "P3", t: "P1" },
      { s: "P2", t: "P1" },
    ],
  };
}

function mountMarkup(): void {
  document.body.innerHTML = `
    <div id="boot-screen"><div id="boot-progress-bar"></div><div id="boot-status"></div></div>
    <div id="menu" hidden>
      <span id="menu-badge-text"></span><h1 id="menu-title"></h1><p id="menu-subtitle"></p>
      <button id="menu-enter"></button><button data-lang="ru"></button><button data-lang="en"></button>
      <div id="menu-theme"></div>
    </div>
    <div id="app" hidden>
      <button id="brand"></button><div id="map"></div><div id="panel"></div>
      <div id="tab-buttons"><button data-tab="1"></button><button data-tab="2"></button><button data-tab="3"></button></div>
      <div id="tab-content"></div><div id="filters-section-label"></div><div id="filter-bar"></div>
    </div>
    <button id="global-search-trigger"></button>
    <div id="global-search" hidden><input id="global-search-input"><div id="global-search-results"></div></div>
    <div id="load-error" hidden></div>
  `;
}

let windowListeners: MockInstance<Window["addEventListener"]>;
let documentListeners: MockInstance<Document["addEventListener"]>;

function cleanPage(): void {
  for (const [type, listener, options] of windowListeners.mock.calls) {
    window.removeEventListener(type, listener, options);
  }
  for (const [type, listener, options] of documentListeners.mock.calls) {
    document.removeEventListener(type, listener, options);
  }
  for (const instance of captured.renderers) {
    instance.removeAllListeners();
    instance.camera.removeAllListeners();
  }
  captured.store = null;
  captured.renderers = [];
  document.body.replaceChildren();
  vi.clearAllTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
}

async function boot(search: string) {
  vi.resetModules();
  mountMarkup();
  history.replaceState(null, "", `/map/${search}`);
  const graph = deferred<Response>();
  const details = {
    pubs: deferred<Response>(),
    authors: deferred<Response>(),
    repos: deferred<Response>(),
  };
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith("graph-data.json")) return graph.promise;
    if (url.endsWith("pubs-detail.json")) return details.pubs.promise;
    if (url.endsWith("authors-detail.json")) return details.authors.promise;
    if (url.endsWith("repos-detail.json")) return details.repos.promise;
    if (url.endsWith("reports/index.json")) return Promise.resolve(response({}));
    if (url.endsWith("implementation-rates.json")) return Promise.resolve(response([]));
    throw new Error(`Unexpected fetch: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  windowListeners = vi.spyOn(window, "addEventListener");
  documentListeners = vi.spyOn(document, "addEventListener");
  vi.spyOn(console, "info").mockReturnValue(undefined);
  vi.spyOn(console, "error").mockReturnValue(undefined);
  vi.spyOn(console, "warn").mockReturnValue(undefined);
  const push = vi.spyOn(history, "pushState");
  const replace = vi.spyOn(history, "replaceState");
  const historyLength = history.length;

  await import("../src/app/main");

  expect(store().get()).toMatchObject({ screen: "menu", tab: 1, selection: null });
  expect(element("menu").hidden).toBe(false);
  expect(element("boot-progress-bar").style.width).toBe("15%");
  expect(location.search).toBe(search);
  expect(replace).not.toHaveBeenCalled();
  expect(push).not.toHaveBeenCalled();
  expect(fetchMock).toHaveBeenCalledTimes(1);

  return {
    graph,
    details,
    push,
    replace,
    historyLength,
    async render(data?: GraphData) {
      graph.resolve(response(data ?? (await graphFixture())));
      await flush();
      expect(console.error).not.toHaveBeenCalled();
      expect(element("boot-progress-bar").style.width).toBe("100%");
      expect(fetchMock).toHaveBeenCalledTimes(6);
      expect(push).not.toHaveBeenCalled();
      expect(replace).toHaveBeenCalledTimes(1);
      expect(history.length).toBe(historyLength);
      vi.advanceTimersByTime(200);
      expect(element("boot-screen").hidden).toBe(true);
    },
    async loadDetails() {
      const pubs = (await loadSamplePubDetails()).map((pub) =>
        pub.key === "P1"
          ? {
              ...pub,
              funding: [{ funder: "Test funder", grant_id: "Test grant", grant_key: "test-grant" }],
            }
          : pub,
      );
      details.pubs.resolve(response(pubs));
      details.authors.resolve(response(await loadSampleAuthorDetails()));
      details.repos.resolve(response(await loadSampleRepoDetails()));
      await flush();
      expect(console.error).not.toHaveBeenCalled();
      return pubs;
    },
  };
}

function expectApp(tab: AppState["tab"], selection: AppState["selection"]): void {
  expect(store().get()).toMatchObject({ screen: "app", tab, selection, lang: "en", theme: "dark" });
  expect(element("app").hidden).toBe(false);
  expect(element("menu").hidden).toBe(true);
  expect(document.querySelector(".tab-button--active")?.getAttribute("data-tab")).toBe(String(tab));
  expect(element("panel").hidden).toBe(false);
}

function filterInput(label: string): HTMLInputElement {
  const row = [...element("filter-bar").querySelectorAll(".filter-row")].find(
    (row) => row.querySelector(".filter-row__label")?.textContent === label,
  );
  const input = row?.querySelector("input");
  if (!input) throw new Error(`Missing filter ${label}`);
  return input;
}

function expectAuthorFilters(external: boolean, isolated: boolean): void {
  expect(store().get().filters).toMatchObject({
    showExternalAuthors: external, showIsolatedAuthors: isolated,
  });
  expect(filterInput(t("filter.showExternal", "en")).checked).toBe(external);
  expect(filterInput(t("filter.showIsolated", "en")).checked).toBe(isolated);
}

function panelRow(label: string): string | undefined {
  return (
    [...element("panel").querySelectorAll("dt")].find((row) => row.textContent === label)
      ?.nextElementSibling?.textContent ?? undefined
  );
}

beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(() => {
  cleanPage();
  vi.useRealTimers();
});

describe("entrypoint URL hydration", () => {
  it.each([
    ["version-only", "P1", "P3", 0],
    ["version-only reversed", "P3", "P1", 0],
    ["mixed", "P1", "P2", 4],
    ["mixed reversed", "P2", "P1", 4],
  ] as const)(
    "cold load and refresh restore a %s publication edge",
    async (_name, s, t, weight) => {
      const first = await boot(`?tab=pubs&sel=edge&s=${s}&t=${t}`);
      await first.render();
      const target = weight === 0 ? "P3" : "P2";
      const selection = { kind: "edge", s: "P1", t: target, w: weight } as const;
      expectApp(3, selection);
      expect(renderer().graph.getEdgeAttribute("P1", target, "weight")).toBe(weight);
      expect(element("panel").textContent).toContain("IS_VERSION");
      expect(panelRow("Weight")).toBe(weight === 0 ? undefined : "4");
      expect(panelRow("From")).toBe(target);
      expect(panelRow("To")).toBe("P1");
      expect(renderer().camera.animate).not.toHaveBeenCalled();
      const canonical = `?tab=pubs&sel=edge&s=P1&t=${target}`;
      expect(location.search).toBe(canonical);
      await first.loadDetails();
      expectApp(3, selection);
      expect(location.search).toBe(canonical);
      expect(first.replace).toHaveBeenCalledTimes(1);

      cleanPage();
      const refreshed = await boot(canonical);
      await refreshed.render();
      expectApp(3, selection);
      expect(renderer().graph.getEdgeAttribute("P1", target, "weight")).toBe(weight);
      expect(location.search).toBe(canonical);
      await refreshed.loadDetails();
      expect(location.search).toBe(canonical);
      expect(refreshed.replace).toHaveBeenCalledTimes(1);
    },
  );

  it.each([
    ["?tab=persons&sel=node&key=A1", 1, { kind: "node", key: "A1" }, "A1"],
    ["?tab=repos&sel=node&key=R1", 2, { kind: "node", key: "R1" }, "R1"],
    ["?tab=pubs&sel=node&key=P1", 3, { kind: "node", key: "P1" }, "P1"],
    ["?tab=persons&sel=dept&id=0", 1, { kind: "dept", id: 0 }, "dept:0"],
    ["?tab=repos&sel=dept&id=9", 2, { kind: "dept", id: 9 }, "dept:9"],
  ] as const)(
    "hydrates node/department/group link %s before camera and panel updates",
    async (search, tab, selection, key) => {
      const page = await boot(search);
      await page.render();
      expectApp(tab, selection);
      expect(renderer().graph.hasNode(key)).toBe(true);
      expect(renderer().camera.animate).toHaveBeenCalledTimes(1);
      expect(renderer().camera.animate.mock.calls[0]?.[0]).toMatchObject(
        renderer().getNodeDisplayData(key) ?? {},
      );
      if (selection.kind === "node") {
        expect(element("tab-content").querySelector(".tab-list-item--selected")).not.toBeNull();
      }
      await page.loadDetails();
      expect(location.search).toBe(search);
      expect(page.replace).toHaveBeenCalledTimes(1);
    },
  );

  it.each([
    ["?tab=persons&sel=edge&s=A2&t=A1", 1, "A1", "A2", 2],
    ["?tab=repos&sel=edge&s=R2&t=R1", 2, "R1", "R2", 1],
  ] as const)("hydrates representative non-publication edge %s", async (search, tab, s, t, w) => {
    const page = await boot(search);
    await page.render();
    expectApp(tab, { kind: "edge", s, t, w });
    expect(renderer().graph.getEdgeAttribute(s, t, "weight")).toBe(w);
    expect(location.search).toBe(`?tab=${tab === 1 ? "persons" : "repos"}&sel=edge&s=${s}&t=${t}`);
  });

  it.each([
    ["AE", true, false, "filter.showExternal"],
    ["AI", false, true, "filter.showIsolated"],
    ["AX", true, true, "filter.showExternal"],
    ["AX", true, true, "filter.showIsolated"],
  ] as const)(
    "reveals hidden author node %s consistently and honors the first checkbox click",
    async (key, external, isolated, clickedFilter) => {
      const search = `?tab=persons&sel=node&key=${key}`;
      const page = await boot(search);
      await page.render();
      expectApp(1, { kind: "node", key });
      expectAuthorFilters(external, isolated);
      expect(renderer().graph.hasNode(key)).toBe(true);
      expect(renderer().camera.animate).toHaveBeenCalledTimes(1);
      expect(location.search).toBe(search);
      const checkbox = filterInput(t(clickedFilter, "en"));
      await page.loadDetails();
      expect(filterInput(t(clickedFilter, "en"))).toBe(checkbox);
      expectAuthorFilters(external, isolated);
      checkbox.click();
      expectAuthorFilters(
        clickedFilter === "filter.showExternal" ? false : external,
        clickedFilter === "filter.showIsolated" ? false : isolated,
      );
      expect(store().get().selection).toBeNull();
      expect(renderer().graph.hasNode(key)).toBe(false);
      expect(location.search).toBe("?tab=persons");
      expect(console.error).not.toHaveBeenCalled();
    },
  );

  it("does not reveal hidden authors for an edge selection", async () => {
    const page = await boot("?tab=persons&sel=edge&s=A1&t=AE");
    await page.render();
    expect(store().get().filters).toMatchObject({
      showExternalAuthors: false,
      showIsolatedAuthors: false,
    });
    expect(renderer().graph.hasNode("AE")).toBe(false);
    expect(renderer().graph.hasEdge("A1", "AE")).toBe(false);
    expectApp(1, { kind: "edge", s: "A1", t: "AE", w: 1 });
    expect(location.search).toBe("?tab=persons&sel=edge&s=A1&t=AE");
  });

  it.each(["", "?tab=menu", "?tab=broken&sel=node&key=A1", "?sel=node&key=A1"])(
    "keeps menu defaults for %s",
    async (search) => {
      const page = await boot(search);
      await page.render();
      expect(store().get()).toMatchObject({
        screen: "menu",
        tab: 1,
        selection: null,
        lang: "en",
        theme: "dark",
        filters: { showExternalAuthors: false, showIsolatedAuthors: false },
      });
      expect(element("menu").hidden).toBe(false);
      expect(element("app").hidden).toBe(true);
      expect(location.search).toBe("?tab=menu");
    },
  );

  it.each(["constructor", "toString", "__proto__", "hasOwnProperty", "unknown"])(
    "keeps malformed tab %s on the menu across boot, refresh and popstate",
    async (tab) => {
      const search = `?tab=${tab}&sel=edge&s=A1&t=A2`;
      const first = await boot(search);
      await first.render();
      expect(store().get()).toMatchObject({ screen: "menu", tab: 1, selection: null });
      expect(location.search).toBe("?tab=menu");
      await first.loadDetails();
      expect(first.replace).toHaveBeenCalledTimes(1);

      cleanPage();
      const refreshed = await boot(search);
      await refreshed.render();
      expect(store().get()).toMatchObject({ screen: "menu", tab: 1, selection: null });
      expect(element("menu").hidden).toBe(false);
      expect(location.search).toBe("?tab=menu");
      element("menu-enter").click();
      document.querySelector<HTMLButtonElement>('[data-tab="2"]')?.click();
      expectApp(2, null);
      const pushes = refreshed.push.mock.calls.length;
      const replacements = refreshed.replace.mock.calls.length;
      history.replaceState(null, "", search);
      const historyLength = history.length;
      window.dispatchEvent(new PopStateEvent("popstate"));
      expect(store().get()).toMatchObject({ screen: "menu", tab: 1, selection: null });
      expect(element("menu").hidden).toBe(false);
      expect(element("app").hidden).toBe(true);
      expect(location.search).toBe(search);
      expect(history.length).toBe(historyLength);
      expect(refreshed.push).toHaveBeenCalledTimes(pushes);
      expect(refreshed.replace).toHaveBeenCalledTimes(replacements + 1);
      await refreshed.loadDetails();
      expect(store().get()).toMatchObject({ screen: "menu", tab: 1, selection: null });
      expect(console.error).not.toHaveBeenCalled();
    },
  );

  it.each([
    "?tab=pubs",
    "?tab=pubs&sel=broken",
    "?tab=pubs&sel=node&key=missing",
    "?tab=pubs&sel=node&key=A1",
    "?tab=pubs&sel=edge&s=P1&t=missing",
    "?tab=pubs&sel=dept&id=999",
  ])("canonicalizes broken/stale selection without losing its tab: %s", async (search) => {
    const page = await boot(search);
    await page.render();
    expectApp(3, null);
    expect(location.search).toBe("?tab=pubs");
  });

  it("keeps pending slider DOM and debounce through deferred details and real changes to other filters", async () => {
    const page = await boot("?tab=persons&sel=node&key=AX");
    await page.render();
    expectAuthorFilters(true, true);
    const coauth = filterInput(t("filter.coauth", "en"));
    const edgeZoom = filterInput(t("filter.edgeZoom", "en"));
    const minNodes = filterInput(t("filter.regionMinNodes", "en"));
    edgeZoom.value = "0.1";
    edgeZoom.dispatchEvent(new Event("input"));
    coauth.focus();
    coauth.value = "7";
    coauth.dispatchEvent(new Event("input"));
    vi.advanceTimersByTime(100);
    await page.loadDetails();
    store().set({ filters: { ...store().get().filters, regionMinNodes: 25 } });
    expect(filterInput(t("filter.coauth", "en"))).toBe(coauth);
    expect(document.activeElement).toBe(coauth);
    expect(coauth.value).toBe("7");
    expect(coauth.dataset.tilt).toBe("right");
    expect(coauth.parentElement?.querySelector(".filter-row__value")?.textContent).toBe("7");
    expect(edgeZoom.value).toBe("0.1");
    expect(minNodes.value).toBe("25");
    expectAuthorFilters(true, true);
    vi.advanceTimersByTime(FILTER_CONFIG.debounceMs - 101);
    expect(store().get().filters.minCoauth).toBe(1);
    vi.advanceTimersByTime(1);
    expect(store().get().filters).toMatchObject({ minCoauth: 7, edgeZoomThreshold: 0.1, regionMinNodes: 25 });
    expectApp(1, { kind: "node", key: "AX" });
    expect(page.replace).toHaveBeenCalledTimes(1);
    expect(console.error).not.toHaveBeenCalled();
  });

  it.each(["tab", "lang"] as const)(
    "cancels pending shared/tab sliders on a real %s change and synchronizes the rebuilt controls",
    async (change) => {
      const page = await boot("?tab=persons&sel=node&key=AX");
      await page.render();
      const coauth = filterInput(t("filter.coauth", "en"));
      const edgeZoom = filterInput(t("filter.edgeZoom", "en"));
      edgeZoom.value = "0.1";
      edgeZoom.dispatchEvent(new Event("input"));
      coauth.value = "7";
      coauth.dispatchEvent(new Event("input"));
      if (change === "tab") document.querySelector<HTMLButtonElement>('[data-tab="3"]')?.click();
      else document.querySelector<HTMLButtonElement>('[data-lang="ru"]')?.click();
      expect(store().get().tab).toBe(change === "tab" ? 3 : 1);
      expect(store().get().lang).toBe(change === "lang" ? "ru" : "en");
      const lang = store().get().lang;
      const rebuilt = filterInput(t("filter.edgeZoom", lang));
      expect(rebuilt).not.toBe(edgeZoom);
      expect(rebuilt.value).toBe(String(FILTER_CONFIG.edgeZoom.default));
      await page.loadDetails();
      vi.advanceTimersByTime(FILTER_CONFIG.debounceMs);
      expect(store().get().filters).toMatchObject({ minCoauth: 1, edgeZoomThreshold: FILTER_CONFIG.edgeZoom.default });
      if (change === "tab") document.querySelector<HTMLButtonElement>('[data-tab="1"]')?.click();
      expect(filterInput(t("filter.showExternal", lang)).checked).toBe(true);
      expect(filterInput(t("filter.showIsolated", lang)).checked).toBe(true);
      expect(filterInput(t("filter.coauth", lang)).value).toBe("1");
      expect(console.error).not.toHaveBeenCalled();
    },
  );

  it("hydrates a grant before details and updates the card without rewriting the URL", async () => {
    const search = "?tab=pubs&sel=grant&key=test-grant";
    const page = await boot(search);
    await page.render();
    expectApp(3, { kind: "grant", key: "test-grant" });
    expect(element("panel").textContent).toContain("test-grant");
    expect(element("panel").querySelector(".loading-indicator")).not.toBeNull();
    await page.loadDetails();
    expect(element("panel").textContent).toContain("Test funder Test grant");
    expect(element("panel").querySelector(".loading-indicator")).toBeNull();
    expect(location.search).toBe(search);
    expect(page.replace).toHaveBeenCalledTimes(1);
  });

  it("keeps the incoming link unchanged when graph-data fails to load", async () => {
    const search = "?tab=pubs&sel=edge&s=P3&t=P1";
    const page = await boot(search);
    page.graph.resolve(response(null, 503));
    await flush();
    expect(element("boot-screen").hidden).toBe(true);
    expect(element("load-error").hidden).toBe(false);
    expect(captured.renderers).toHaveLength(0);
    expect(location.search).toBe(search);
    expect(page.replace).not.toHaveBeenCalled();
    expect(page.push).not.toHaveBeenCalled();
    expect(history.length).toBe(page.historyLength);
    expect(console.error).toHaveBeenCalled();
  });

  it("detail failures leave the selected publication and its URL intact", async () => {
    const search = "?tab=pubs&sel=node&key=P1";
    const page = await boot(search);
    await page.render();
    page.details.pubs.resolve(response(null, 404));
    page.details.authors.resolve(response(null, 404));
    page.details.repos.resolve(response(null, 404));
    await flush();
    expectApp(3, { kind: "node", key: "P1" });
    expect(element("panel").querySelector("h3")?.textContent).toBe("P1");
    expect(element("load-error").hidden).toBe(true);
    expect(location.search).toBe(search);
    expect(page.replace).toHaveBeenCalledTimes(1);
    expect(page.push).not.toHaveBeenCalled();
    expect(console.error).toHaveBeenCalledTimes(3);
  });

  it("preserves replace/push semantics and restores Back/Forward without new entries", async () => {
    const incoming = "?tab=pubs&sel=edge&s=P1&t=P3";
    const page = await boot(incoming);
    await page.render();
    document.querySelector<HTMLButtonElement>('[data-tab="2"]')?.click();
    expectApp(2, null);
    expect(page.push).toHaveBeenCalledTimes(1);
    expect(location.search).toBe("?tab=repos");
    const edge = renderer().graph.edge("R1", "R2") as string;
    renderer().emit("clickEdge", { edge, event: { x: 0, y: 0 } });
    expectApp(2, { kind: "edge", s: "R1", t: "R2", w: 1 });
    expect(page.replace).toHaveBeenCalledTimes(2);
    expect(history.length).toBe(page.historyLength + 1);
    const repoUrl = location.search;

    vi.useRealTimers();
    const traverse = async (direction: "back" | "forward", search: string) => {
      const popped = new Promise<void>((resolve) => {
        window.addEventListener("popstate", () => resolve(), { once: true });
      });
      history[direction]();
      await popped;
      expect(location.search).toBe(search);
      expect(page.replace).toHaveBeenCalledTimes(2);
      expect(page.push).toHaveBeenCalledTimes(1);
      expect(history.length).toBe(page.historyLength + 1);
    };
    await traverse("back", incoming);
    expectApp(3, { kind: "edge", s: "P1", t: "P3", w: 0 });
    await traverse("forward", repoUrl);
    expectApp(2, { kind: "edge", s: "R1", t: "R2", w: 1 });
    element("brand").click();
    expect(store().get().screen).toBe("menu");
    expect(location.search).toBe("?tab=menu");
    expect(page.push).toHaveBeenCalledTimes(2);
  });
});
