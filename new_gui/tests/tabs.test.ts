import type { Map as MapLibreMap } from "maplibre-gl";
import { describe, expect, it, vi } from "vitest";
import { loadSampleGraphData } from "../src/core/data";
import { Store, type AppState } from "../src/core/state";
import { mountTabs } from "../src/features/tabs";
import { authorsTab } from "../src/features/tabs/authors";
import { pubsTab } from "../src/features/tabs/pubs";
import { reposTab } from "../src/features/tabs/repos";

/**
 * Вкладкам от карты нужен только flyTo() (вызывается по клику на элемент
 * списка) — настоящий MapLibre в jsdom не поднять (ему нужен WebGL-канвас),
 * поэтому подставляем минимальную заглушку вместо реальной карты.
 */
function fakeMap(): MapLibreMap {
  return { flyTo: vi.fn() } as unknown as MapLibreMap;
}

function initialState(): AppState {
  return {
    tab: 1,
    lang: "ru",
    selection: null,
    filters: { minCoauth: 1, minPubAuthors: 1, yearMax: 2026 },
    stats: { busy: false, error: null },
  };
}

describe("authorsTab", () => {
  it("отрисовывает авторов по убыванию pubs_count и подсвечивает выбранного", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    const container = document.createElement("div");

    authorsTab.mount(container, store, fakeMap(), data);

    const sorted = [...data.authors].sort((a, b) => b.pubs_count - a.pubs_count);
    expect(Array.from(container.children).map((el) => el.textContent)).toEqual(
      sorted.map((a) => `${a.label}${a.pubs_count}`),
    );

    const first = sorted[0];
    if (!first) throw new Error("во фикстуре должен быть хотя бы один автор");
    store.set({ selection: { kind: "node", key: first.key } });

    expect(container.firstElementChild?.classList.contains("tab-list-item--selected")).toBe(true);
  });

  it("клик по автору пишет выбор в store и подлетает к нему на карте", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    const container = document.createElement("div");
    const map = fakeMap();

    authorsTab.mount(container, store, map, data);
    const firstItem = container.firstElementChild as HTMLButtonElement;
    firstItem.click();

    const author = [...data.authors].sort((a, b) => b.pubs_count - a.pubs_count)[0];
    expect(store.get().selection).toEqual({ kind: "node", key: author?.key });
    expect(map.flyTo).toHaveBeenCalledOnce();
  });
});

describe("reposTab", () => {
  it("сортирует репозитории по звёздам по убыванию", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    const container = document.createElement("div");

    reposTab.mount(container, store, fakeMap(), data);

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

    mountTabs(buttons, content, store, fakeMap(), data);

    expect(content.children.length).toBe(data.authors.length);
    expect(buttons.querySelector('[data-tab="1"]')?.classList.contains("tab-button--active")).toBe(true);
  });

  it("клик по кнопке вкладки размонтирует старую и монтирует новую", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    const buttons = buttonsMarkup();
    const content = document.createElement("div");

    mountTabs(buttons, content, store, fakeMap(), data);
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

    pubsTab.mount(container, store, fakeMap(), data);

    const years = Array.from(container.children).map((el) => el.textContent?.includes("неизвестен"));
    // Как только встретили "год неизвестен", все последующие тоже должны быть без года.
    const firstUnknownIndex = years.indexOf(true);
    if (firstUnknownIndex !== -1) {
      expect(years.slice(firstUnknownIndex).every(Boolean)).toBe(true);
    }
  });
});
