import { beforeEach, describe, expect, it } from "vitest";
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
    selection: null,
    filters: { minCoauth: 1, minSharedAuthors: 1, yearMax: 2026, showNoDeptAuthors: true, showNoDeptPubs: true },
    ...overrides,
  };
}

/** Минимальная разметка — ровно те id, которые requireElement() ищет внутри mountGlobalSearch(). */
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

  it("клик по кнопке-триггеру открывает окно и переводит фокус в поле ввода", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    mountGlobalSearch(store, data, NO_PUB_DETAILS, NO_REPO_DETAILS);

    document.getElementById("global-search-trigger")?.dispatchEvent(new MouseEvent("click", { bubbles: true }));

    const overlay = document.getElementById("global-search") as HTMLElement;
    const input = document.getElementById("global-search-input") as HTMLInputElement;
    expect(overlay.hidden).toBe(false);
    expect(document.activeElement).toBe(input);
    expect(input.placeholder).toBe("Поиск по авторам, репозиториям, публикациям, департаментам…");
  });

  it("клавиша '/' открывает окно, если фокус не в текстовом поле", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    mountGlobalSearch(store, data, NO_PUB_DETAILS, NO_REPO_DETAILS);

    document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "/", bubbles: true }));

    expect((document.getElementById("global-search") as HTMLElement).hidden).toBe(false);
  });

  it("клавиша '/' НЕ открывает окно, если фокус в текстовом поле — иначе перехватывала бы обычный ввод символа", async () => {
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

  it("Escape закрывает открытое окно", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    mountGlobalSearch(store, data, NO_PUB_DETAILS, NO_REPO_DETAILS);

    document.getElementById("global-search-trigger")?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));

    expect((document.getElementById("global-search") as HTMLElement).hidden).toBe(true);
  });

  it("клик по затемнённому фону (не по самому окну) закрывает поиск", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    mountGlobalSearch(store, data, NO_PUB_DETAILS, NO_REPO_DETAILS);

    const overlay = document.getElementById("global-search") as HTMLElement;
    document.getElementById("global-search-trigger")?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    overlay.dispatchEvent(new MouseEvent("click", { bubbles: true })); // target === overlay сам по себе

    expect(overlay.hidden).toBe(true);
  });

  it("выбор автора из результатов переключает вкладку на 1 и пишет selection", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState({ tab: 2 })); // намеренно НЕ на вкладке автора
    mountGlobalSearch(store, data, NO_PUB_DETAILS, NO_REPO_DETAILS);

    document.getElementById("global-search-trigger")?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    const input = document.getElementById("global-search-input") as HTMLInputElement;
    input.value = "Иванов";
    input.dispatchEvent(new Event("input"));

    const hit = document.querySelector<HTMLButtonElement>("#global-search-results .tab-list-item");
    if (!hit) throw new Error("должен найтись хотя бы один результат для 'Иванов'");
    hit.click();

    expect(store.get().screen).toBe("app");
    expect(store.get().tab).toBe(1);
    expect(store.get().selection).toEqual({ kind: "node", key: "A1" });
    expect((document.getElementById("global-search") as HTMLElement).hidden).toBe(true); // закрылось после выбора
  });

  it("выбор департамента пишет selection dept, не трогая tab", async () => {
    const data = await loadSampleGraphData();
    const dept = data.departments[0];
    if (!dept) throw new Error("фикстура должна содержать хотя бы один департамент");
    const store = new Store<AppState>(initialState({ tab: 3 }));
    mountGlobalSearch(store, data, NO_PUB_DETAILS, NO_REPO_DETAILS);

    document.getElementById("global-search-trigger")?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    const input = document.getElementById("global-search-input") as HTMLInputElement;
    input.value = dept.name;
    input.dispatchEvent(new Event("input"));

    const hit = [...document.querySelectorAll<HTMLButtonElement>("#global-search-results .tab-list-item")].find(
      (button) => button.dataset.kind === "dept",
    );
    if (!hit) throw new Error(`должен найтись департамент "${dept.name}" среди результатов`);
    hit.click();

    expect(store.get().tab).toBe(3); // не менялась
    expect(store.get().selection).toEqual({ kind: "dept", id: dept.id });
  });

  it("запрос без совпадений показывает 'Ничего не найдено'", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    mountGlobalSearch(store, data, NO_PUB_DETAILS, NO_REPO_DETAILS);

    document.getElementById("global-search-trigger")?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    const input = document.getElementById("global-search-input") as HTMLInputElement;
    input.value = "лщывалщыв"; // заведомо не встречается ни в одной подписи фикстуры
    input.dispatchEvent(new Event("input"));

    expect(document.querySelectorAll("#global-search-results .tab-list-item")).toHaveLength(0);
    expect(document.querySelector("#global-search-results .tab-empty")?.textContent).toBe("Ничего не найдено");
  });

  it("пустой запрос (сразу после открытия) не показывает 'Ничего не найдено' — просто пустой список", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    mountGlobalSearch(store, data, NO_PUB_DETAILS, NO_REPO_DETAILS);

    document.getElementById("global-search-trigger")?.dispatchEvent(new MouseEvent("click", { bubbles: true }));

    expect(document.querySelector("#global-search-results .tab-empty")).toBeNull();
  });
});
