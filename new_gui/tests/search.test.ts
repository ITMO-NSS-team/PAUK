import { describe, expect, it, vi } from "vitest";
import type { Map as MapLibreMap } from "maplibre-gl";
import { loadSampleGraphData } from "../src/core/data";
import { Store, type AppState } from "../src/core/state";
import { buildSearchIndex, deptHitKey, parseDeptHitKey, searchHits } from "../src/features/search";
import { searchTab } from "../src/features/tabs/search";

function fakeMap(): MapLibreMap {
  return { flyTo: vi.fn() } as unknown as MapLibreMap;
}

function initialState(): AppState {
  return {
    tab: 4,
    lang: "ru",
    selection: null,
    filters: { minCoauth: 1, minPubAuthors: 1, yearMax: 2026 },
  };
}

describe("deptHitKey / parseDeptHitKey", () => {
  it("парсинг возвращает то же число, что было закодировано", () => {
    for (const id of [0, 1, 42]) {
      expect(parseDeptHitKey(deptHitKey(id))).toBe(id);
    }
  });
});

describe("buildSearchIndex", () => {
  it("включает все виды сущностей: авторов, репозитории, публикации, департаменты", async () => {
    const data = await loadSampleGraphData();
    const index = buildSearchIndex(data, "ru");

    const total = data.authors.length + data.repos.length + data.pubs.length + data.departments.length;
    expect(index).toHaveLength(total);
    expect(index.some((hit) => hit.kind === "dept")).toBe(true);
  });
});

describe("searchHits", () => {
  it("пустой запрос — пустой список результатов, а не всё подряд", async () => {
    const data = await loadSampleGraphData();
    const index = buildSearchIndex(data, "ru");
    expect(searchHits(index, "")).toEqual([]);
    expect(searchHits(index, "   ")).toEqual([]);
  });

  it("находит по подстроке в label без учёта регистра", async () => {
    const data = await loadSampleGraphData();
    const index = buildSearchIndex(data, "ru");
    const author = data.authors[0];
    if (!author) throw new Error("фикстура должна содержать хотя бы одного автора");

    const hits = searchHits(index, author.label.slice(0, 3).toUpperCase());
    expect(hits.some((hit) => hit.key === author.key)).toBe(true);
  });
});

describe("searchTab", () => {
  it("ввод текста фильтрует список результатов", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    const container = document.createElement("div");

    searchTab.mount(container, store, fakeMap(), data);
    const results = container.querySelector(".search-results") as HTMLElement;
    expect(results.children).toHaveLength(0);

    const input = container.querySelector("input") as HTMLInputElement;
    const author = data.authors[0];
    if (!author) throw new Error("фикстура должна содержать хотя бы одного автора");
    input.value = author.label;
    input.dispatchEvent(new Event("input"));

    expect(results.children.length).toBeGreaterThan(0);
  });

  it("клик по результату-департаменту пишет selection dept, без flyTo (у департамента нет своих координат)", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    const container = document.createElement("div");
    const map = fakeMap();

    searchTab.mount(container, store, map, data);
    const input = container.querySelector("input") as HTMLInputElement;
    const dept = data.departments[0];
    if (!dept) throw new Error("фикстура должна содержать хотя бы один департамент");
    input.value = dept.name;
    input.dispatchEvent(new Event("input"));

    // Поиск по имени департамента находит и авторов из него самого (их sub
    // содержит имя департамента), поэтому берём именно результат-департамент
    // по data-kind, а не полагаемся на порядок в списке.
    const results = container.querySelector(".search-results") as HTMLElement;
    const deptButton = results.querySelector('[data-kind="dept"]') as HTMLButtonElement;
    deptButton.click();

    expect(store.get().selection).toEqual({ kind: "dept", id: dept.id });
    expect(map.flyTo).not.toHaveBeenCalled();
  });
});
