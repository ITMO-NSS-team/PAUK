import type { Map as MapLibreMap } from "maplibre-gl";
import { describe, expect, it, vi } from "vitest";
import type { PubDetail, RepoDetail } from "../src/contracts/graph";
import { indexDetailsByKey } from "../src/core/data";
import { Store, type AppState } from "../src/core/state";
import { buildSearchIndex, deptHitKey, parseDeptHitKey, searchHits } from "../src/features/search";
import { searchTab } from "../src/features/tabs/search";
import { loadSampleGraphData, loadSamplePubDetails, loadSampleRepoDetails } from "./fixtures";

const NO_PUB_DETAILS = new Map<string, PubDetail>();
const NO_REPO_DETAILS = new Map<string, RepoDetail>();

function fakeMap(): MapLibreMap {
  return { flyTo: vi.fn() } as unknown as MapLibreMap;
}

function initialState(): AppState {
  return {
    tab: 4,
    lang: "ru",
    selection: null,
    filters: { minCoauth: 1, minSharedAuthors: 1, yearMax: 2026 },
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
    const index = buildSearchIndex(data, "ru", NO_PUB_DETAILS, NO_REPO_DETAILS);

    const total = data.authors.length + data.repos.length + data.pubs.length + data.departments.length;
    expect(index).toHaveLength(total);
    expect(index.some((hit) => hit.kind === "dept")).toBe(true);
  });

  it("для публикаций использует настоящее название и добавляет журнал в sub, когда есть pubDetails", async () => {
    const data = await loadSampleGraphData();
    const pubDetails = indexDetailsByKey(await loadSamplePubDetails());
    const index = buildSearchIndex(data, "ru", pubDetails, NO_REPO_DETAILS);

    for (const pub of data.pubs) {
      const hit = index.find((h) => h.kind === "pub" && h.key === pub.key);
      const detail = pubDetails.get(pub.key);
      expect(hit?.label).toBe(detail?.label);
      expect(hit?.sub).toContain(detail?.journal);
    }
  });

  it("для репозиториев берёт короткий путь на GitHub из repoDetails (url больше не на RepoNode)", async () => {
    const data = await loadSampleGraphData();
    const repoDetails = indexDetailsByKey(await loadSampleRepoDetails());
    const index = buildSearchIndex(data, "ru", NO_PUB_DETAILS, repoDetails);

    for (const repo of data.repos) {
      const hit = index.find((h) => h.kind === "repo" && h.key === repo.key);
      const url = repoDetails.get(repo.key)?.url ?? "";
      expect(hit?.sub).toBe(url.replace("https://github.com/", ""));
    }
  });

  it("для репозитория без записи в repoDetails sub — null, а не падение", async () => {
    const data = await loadSampleGraphData();
    const index = buildSearchIndex(data, "ru", NO_PUB_DETAILS, NO_REPO_DETAILS);

    const hit = index.find((h) => h.kind === "repo");
    expect(hit?.sub).toBeNull();
  });
});

describe("searchHits", () => {
  it("пустой запрос — пустой список результатов, а не всё подряд", async () => {
    const data = await loadSampleGraphData();
    const index = buildSearchIndex(data, "ru", NO_PUB_DETAILS, NO_REPO_DETAILS);
    expect(searchHits(index, "")).toEqual([]);
    expect(searchHits(index, "   ")).toEqual([]);
  });

  it("находит по подстроке в label без учёта регистра", async () => {
    const data = await loadSampleGraphData();
    const index = buildSearchIndex(data, "ru", NO_PUB_DETAILS, NO_REPO_DETAILS);
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

    searchTab.mount(container, store, fakeMap(), data, NO_PUB_DETAILS, NO_REPO_DETAILS);
    const results = container.querySelector(".search-results") as HTMLElement;
    expect(results.children).toHaveLength(0);

    const input = container.querySelector("input") as HTMLInputElement;
    const author = data.authors[0];
    if (!author) throw new Error("фикстура должна содержать хотя бы одного автора");
    input.value = author.label;
    input.dispatchEvent(new Event("input"));

    expect(results.children.length).toBeGreaterThan(0);
  });

  it("клик по результату-департаменту пишет selection dept, без flyTo (у департамента нет своих координат) и без переключения вкладки", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    const container = document.createElement("div");
    const map = fakeMap();

    searchTab.mount(container, store, map, data, NO_PUB_DETAILS, NO_REPO_DETAILS);
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
    expect(store.get().tab).toBe(4); // у департамента нет своей вкладки с графом — вкладку не трогаем
  });

  it("клик по результату-автору переключает вкладку на 1 (иначе выбор невидим — карта на вкладке 4 пуста)", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    const container = document.createElement("div");
    const map = fakeMap();

    searchTab.mount(container, store, map, data, NO_PUB_DETAILS, NO_REPO_DETAILS);
    const input = container.querySelector("input") as HTMLInputElement;
    const author = data.authors[0];
    if (!author) throw new Error("фикстура должна содержать хотя бы одного автора");
    input.value = author.label;
    input.dispatchEvent(new Event("input"));

    const results = container.querySelector(".search-results") as HTMLElement;
    const authorButton = results.querySelector('[data-kind="author"]') as HTMLButtonElement;
    authorButton.click();

    expect(store.get().tab).toBe(1);
    expect(store.get().selection).toEqual({ kind: "node", key: author.key });
    expect(map.flyTo).toHaveBeenCalledOnce();
  });

  it("клик по результату-репозиторию переключает вкладку на 2, по результату-публикации — на 3", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    const container = document.createElement("div");
    const map = fakeMap();

    searchTab.mount(container, store, map, data, NO_PUB_DETAILS, NO_REPO_DETAILS);
    const input = container.querySelector("input") as HTMLInputElement;
    const results = container.querySelector(".search-results") as HTMLElement;

    const repo = data.repos[0];
    if (!repo) throw new Error("фикстура должна содержать хотя бы один репозиторий");
    input.value = repo.label;
    input.dispatchEvent(new Event("input"));
    (results.querySelector('[data-kind="repo"]') as HTMLButtonElement).click();
    expect(store.get().tab).toBe(2);

    const pub = data.pubs[0];
    if (!pub) throw new Error("фикстура должна содержать хотя бы одну публикацию");
    input.value = pub.key;
    input.dispatchEvent(new Event("input"));
    (results.querySelector('[data-kind="pub"]') as HTMLButtonElement).click();
    expect(store.get().tab).toBe(3);
  });
});
