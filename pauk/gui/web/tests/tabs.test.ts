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

/**
 * Вкладкам-спискам (createNodeListTab) от рендерера сейчас не нужно вообще
 * ничего — камерой к выбранному подлетает централизованно
 * map/build.ts::mountReactiveGraph, а не сама вкладка, — но параметр есть
 * в общем контракте TabModule.mount() (см. features/tabs/types.ts), поэтому
 * заглушка остаётся, просто пустая.
 */
function fakeRenderer(): Sigma {
  return {} as unknown as Sigma;
}

function initialState(overrides: Partial<AppState> = {}): AppState {
  return {
    screen: "app",
    tab: 1,
    lang: "ru",
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

/** Строки списка вкладки — второй ребёнок контейнера (первый — поле поиска, см. createNodeListTab). */
function listItems(container: HTMLElement): HTMLButtonElement[] {
  return Array.from(container.querySelectorAll<HTMLButtonElement>(".tab-list-item"));
}

/**
 * Синтетический `GraphData` с `count` авторами — фикстура `./fixtures.ts`
 * (8 авторов) намеренно не трогается ради этих тестов (короче
 * TAB_LIST_CONFIG.pageSize, пагинация там никогда не появляется вовсе), а
 * тесты постраничного списка нужны как раз на данных БОЛЬШЕ одной страницы.
 * `pubs_count: count - i` — по убыванию вместе с индексом, поэтому порядок
 * после сортировки (authorsTab::compare — по убыванию pubs_count) точно
 * совпадает с порядком индексов (0, 1, 2, ...), удобно для предсказуемых
 * ассертов "что на какой странице".
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
  it("отрисовывает авторов по убыванию pubs_count и подсвечивает выбранного", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    const container = document.createElement("div");

    authorsTab.mount(container, store, fakeRenderer(), data, NO_PUB_DETAILS, NO_REPO_DETAILS);

    const sorted = [...data.authors].sort((a, b) => b.pubs_count - a.pubs_count);
    expect(listItems(container).map((el) => el.textContent)).toEqual(
      sorted.map((a) => `${a.label}${a.pubs_count}`),
    );

    const first = sorted[0];
    if (!first) throw new Error("во фикстуре должен быть хотя бы один автор");
    store.set({ selection: { kind: "node", key: first.key } });

    expect(listItems(container)[0]?.classList.contains("tab-list-item--selected")).toBe(true);
  });

  it("внешние авторы появляются в списке только с включённым фильтром", async () => {
    const sample = await loadSampleGraphData();
    const [first, ...rest] = [...sample.authors].sort((a, b) => b.pubs_count - a.pubs_count);
    if (!first) throw new Error("во фикстуре должен быть хотя бы один автор");
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

  it("переключение lang на en показывает label_en вместо label", async () => {
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

  it("клик по автору пишет выбор в store — камерой подлетает map/build.ts, не сама вкладка", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    const container = document.createElement("div");

    authorsTab.mount(container, store, fakeRenderer(), data, NO_PUB_DETAILS, NO_REPO_DETAILS);
    listItems(container)[0]?.click();

    const author = [...data.authors].sort((a, b) => b.pubs_count - a.pubs_count)[0];
    expect(store.get().selection).toEqual({ kind: "node", key: author?.key });
  });

  it("поле поиска фильтрует список по вхождению подстроки в подпись, без учёта регистра", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    const container = document.createElement("div");

    authorsTab.mount(container, store, fakeRenderer(), data, NO_PUB_DETAILS, NO_REPO_DETAILS);
    const search = container.querySelector<HTMLInputElement>(".tab-search");
    if (!search) throw new Error("вкладка должна содержать поле поиска");

    search.value = "иванов";
    search.dispatchEvent(new Event("input"));

    expect(listItems(container).map((el) => el.textContent)).toEqual(["Иванов И.И.4"]);
  });

  it("пустой запрос поиска снова показывает весь список", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    const container = document.createElement("div");

    authorsTab.mount(container, store, fakeRenderer(), data, NO_PUB_DETAILS, NO_REPO_DETAILS);
    const search = container.querySelector<HTMLInputElement>(".tab-search");
    if (!search) throw new Error("вкладка должна содержать поле поиска");

    search.value = "иванов";
    search.dispatchEvent(new Event("input"));
    search.value = "";
    search.dispatchEvent(new Event("input"));

    expect(listItems(container)).toHaveLength(data.authors.length);
  });

  it("запрос без совпадений показывает 'Ничего не найдено', а не пустой список", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    const container = document.createElement("div");

    authorsTab.mount(container, store, fakeRenderer(), data, NO_PUB_DETAILS, NO_REPO_DETAILS);
    const search = container.querySelector<HTMLInputElement>(".tab-search");
    if (!search) throw new Error("вкладка должна содержать поле поиска");

    search.value = "лщывалщыв"; // заведомо не встречается ни в одной подписи фикстуры
    search.dispatchEvent(new Event("input"));

    expect(listItems(container)).toHaveLength(0);
    expect(container.querySelector(".tab-empty")?.textContent).toBe("Ничего не найдено");
  });
});

describe("reposTab", () => {
  it("сортирует репозитории по звёздам по убыванию", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    const container = document.createElement("div");

    reposTab.mount(container, store, fakeRenderer(), data, NO_PUB_DETAILS, NO_REPO_DETAILS);

    const stars = listItems(container).map((el) => Number(el.textContent?.match(/\d+/)?.[0]));
    expect(stars).toEqual([...stars].sort((a, b) => b - a));
  });
});

describe("mountTabs — переключение вкладок", () => {
  function buttonsMarkup(): HTMLElement {
    const nav = document.createElement("nav");
    nav.innerHTML = `
      <button type="button" data-tab="1">Авторы</button>
      <button type="button" data-tab="2">Репозитории</button>
      <button type="button" data-tab="3">Публикации</button>
    `;
    return nav;
  }

  it("по умолчанию монтирует вкладку 1 (авторы) и подсвечивает её кнопку", async () => {
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

  it("клик по кнопке вкладки размонтирует старую и монтирует новую", async () => {
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

  it("клик по кнопке вкладки не трогает selection сам по себе — обнулять устаревший выбор при пересборке графа умеет map/build.ts::mountReactiveGraph (см. tests/build.test.ts)", async () => {
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
  it("публикации без года (year === null) идут в конце списка", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    const container = document.createElement("div");

    pubsTab.mount(container, store, fakeRenderer(), data, NO_PUB_DETAILS, NO_REPO_DETAILS);

    const years = listItems(container).map((el) => el.textContent?.includes("неизвестен"));
    // Как только встретили "год неизвестен", все последующие тоже должны быть без года.
    const firstUnknownIndex = years.indexOf(true);
    if (firstUnknownIndex !== -1) {
      expect(years.slice(firstUnknownIndex).every(Boolean)).toBe(true);
    }
  });

  it("показывает настоящее название публикации из pubDetails вместо ключа", async () => {
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

describe("createNodeListTab — постраничный список (TAB_LIST_CONFIG.pageSize)", () => {
  it("показывает подпись-раздел «Быстрый поиск»/«Quick Search» под текущий язык", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState({ lang: "ru" }));
    const container = document.createElement("div");

    authorsTab.mount(container, store, fakeRenderer(), data, NO_PUB_DETAILS, NO_REPO_DETAILS);
    expect(container.querySelector(".sidebar-section-label")?.textContent).toBe("Быстрый поиск");

    store.set({ lang: "en" });
    expect(container.querySelector(".sidebar-section-label")?.textContent).toBe("Quick Search");
  });

  it("режет список до TAB_LIST_CONFIG.pageSize строк за раз и листает через '‹'/'›'", () => {
    const data = manyAuthorsData(25);
    const store = new Store<AppState>(initialState());
    const container = document.createElement("div");

    authorsTab.mount(container, store, fakeRenderer(), data, NO_PUB_DETAILS, NO_REPO_DETAILS);

    expect(listItems(container)).toHaveLength(10);
    // pubs_count по убыванию = порядок индексов — первая страница это A0..A9.
    expect(listItems(container)[0]?.textContent).toContain("Автор 00");
    expect(container.querySelector(".tab-pagination__status")?.textContent).toBe("1 / 3"); // 25 / 10 = 3 страницы

    const next = container.querySelector<HTMLButtonElement>(
      '.tab-pagination__button[aria-label="Следующая страница"]',
    );
    if (!next) throw new Error("должна быть кнопка 'следующая страница'");
    next.click();

    expect(listItems(container)).toHaveLength(10);
    expect(listItems(container)[0]?.textContent).toContain("Автор 10");
    expect(container.querySelector(".tab-pagination__status")?.textContent).toBe("2 / 3");

    const prev = container.querySelector<HTMLButtonElement>(
      '.tab-pagination__button[aria-label="Предыдущая страница"]',
    );
    if (!prev) throw new Error("должна быть кнопка 'предыдущая страница'");
    prev.click();

    expect(listItems(container)[0]?.textContent).toContain("Автор 00");
    expect(container.querySelector(".tab-pagination__status")?.textContent).toBe("1 / 3");
  });

  it("новый поисковый запрос сбрасывает страницу на первую", () => {
    const data = manyAuthorsData(25);
    const store = new Store<AppState>(initialState());
    const container = document.createElement("div");

    authorsTab.mount(container, store, fakeRenderer(), data, NO_PUB_DETAILS, NO_REPO_DETAILS);
    container
      .querySelector<HTMLButtonElement>('.tab-pagination__button[aria-label="Следующая страница"]')
      ?.click();
    expect(container.querySelector(".tab-pagination__status")?.textContent).toBe("2 / 3");

    const search = container.querySelector<HTMLInputElement>(".tab-search");
    if (!search) throw new Error("вкладка должна содержать поле поиска");
    // Совпадает ровно с "Автор 10".."Автор 19" — 10 штук, ровно одна страница.
    search.value = "Автор 1";
    search.dispatchEvent(new Event("input"));

    expect(listItems(container)).toHaveLength(10);
    // Одна страница — контролов пагинации нет вовсе (не просто задизейблены).
    expect(container.querySelector(".tab-pagination")?.children).toHaveLength(0);
  });

  it("короткий список (меньше pageSize) не показывает пагинацию вовсе", async () => {
    const data = await loadSampleGraphData(); // во фикстуре 8 авторов — меньше TAB_LIST_CONFIG.pageSize
    const store = new Store<AppState>(initialState());
    const container = document.createElement("div");

    authorsTab.mount(container, store, fakeRenderer(), data, NO_PUB_DETAILS, NO_REPO_DETAILS);

    expect(container.querySelector(".tab-pagination")?.children).toHaveLength(0);
  });
});
