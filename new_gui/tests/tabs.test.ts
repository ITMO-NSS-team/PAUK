import type Sigma from "sigma";
import { describe, expect, it, vi } from "vitest";
import type { PubDetail, RepoDetail } from "../src/contracts/graph";
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
 * Вкладкам от рендерера нужна только camera.animate() (вызывается по клику
 * на элемент списка) — настоящий Sigma в jsdom не поднять (нужен
 * WebGL-канвас), поэтому подставляем минимальную заглушку.
 */
function fakeRenderer(): { renderer: Sigma; animate: ReturnType<typeof vi.fn> } {
  const animate = vi.fn();
  const renderer = { getCamera: () => ({ animate }) } as unknown as Sigma;
  return { renderer, animate };
}

function initialState(): AppState {
  return {
    tab: 1,
    lang: "ru",
    selection: null,
    filters: { minCoauth: 1, minSharedAuthors: 1, yearMax: 2026 },
  };
}

describe("authorsTab", () => {
  it("отрисовывает авторов по убыванию pubs_count и подсвечивает выбранного", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    const container = document.createElement("div");

    authorsTab.mount(container, store, fakeRenderer().renderer, data, NO_PUB_DETAILS, NO_REPO_DETAILS);

    const sorted = [...data.authors].sort((a, b) => b.pubs_count - a.pubs_count);
    expect(Array.from(container.children).map((el) => el.textContent)).toEqual(
      sorted.map((a) => `${a.label}${a.pubs_count}`),
    );

    const first = sorted[0];
    if (!first) throw new Error("во фикстуре должен быть хотя бы один автор");
    store.set({ selection: { kind: "node", key: first.key } });

    expect(container.firstElementChild?.classList.contains("tab-list-item--selected")).toBe(true);
  });

  it("переключение lang на en показывает label_en вместо label", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    const container = document.createElement("div");

    authorsTab.mount(container, store, fakeRenderer().renderer, data, NO_PUB_DETAILS, NO_REPO_DETAILS);
    store.set({ lang: "en" });

    const sorted = [...data.authors].sort((a, b) => b.pubs_count - a.pubs_count);
    expect(Array.from(container.children).map((el) => el.textContent)).toEqual(
      sorted.map((a) => `${a.label_en}${a.pubs_count}`),
    );
  });

  it("клик по автору пишет выбор в store и подлетает к нему на карте", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    const container = document.createElement("div");
    const { renderer, animate } = fakeRenderer();

    authorsTab.mount(container, store, renderer, data, NO_PUB_DETAILS, NO_REPO_DETAILS);
    const firstItem = container.firstElementChild as HTMLButtonElement;
    firstItem.click();

    const author = [...data.authors].sort((a, b) => b.pubs_count - a.pubs_count)[0];
    expect(store.get().selection).toEqual({ kind: "node", key: author?.key });
    expect(animate).toHaveBeenCalledOnce();
  });
});

describe("reposTab", () => {
  it("сортирует репозитории по звёздам по убыванию", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    const container = document.createElement("div");

    reposTab.mount(container, store, fakeRenderer().renderer, data, NO_PUB_DETAILS, NO_REPO_DETAILS);

    const stars = Array.from(container.children).map((el) => Number(el.textContent?.match(/\d+/)?.[0]));
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

    mountTabs(buttons, content, store, fakeRenderer().renderer, data, NO_PUB_DETAILS, NO_REPO_DETAILS);

    expect(content.children.length).toBe(data.authors.length);
    expect(buttons.querySelector('[data-tab="1"]')?.classList.contains("tab-button--active")).toBe(true);
  });

  it("клик по кнопке вкладки размонтирует старую и монтирует новую", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    const buttons = buttonsMarkup();
    const content = document.createElement("div");

    mountTabs(buttons, content, store, fakeRenderer().renderer, data, NO_PUB_DETAILS, NO_REPO_DETAILS);
    (buttons.querySelector('[data-tab="2"]') as HTMLButtonElement).click();

    expect(store.get().tab).toBe(2);
    expect(content.children.length).toBe(data.repos.length);
    expect(buttons.querySelector('[data-tab="2"]')?.classList.contains("tab-button--active")).toBe(true);
    expect(buttons.querySelector('[data-tab="1"]')?.classList.contains("tab-button--active")).toBe(false);
  });
});

describe("pubsTab", () => {
  it("публикации без года (year === null) идут в конце списка", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    const container = document.createElement("div");

    pubsTab.mount(container, store, fakeRenderer().renderer, data, NO_PUB_DETAILS, NO_REPO_DETAILS);

    const years = Array.from(container.children).map((el) => el.textContent?.includes("неизвестен"));
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

    pubsTab.mount(container, store, fakeRenderer().renderer, data, pubDetails, NO_REPO_DETAILS);

    const labels = Array.from(container.querySelectorAll(".tab-list-item__label")).map((el) => el.textContent);
    for (const pub of data.pubs) {
      expect(labels).toContain(pubDetails.get(pub.key)?.label);
    }
  });
});
