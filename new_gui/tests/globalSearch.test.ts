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
    filters: {
      minCoauth: 1,
      minSharedAuthors: 1,
      yearMax: 2026,
      showNoDeptAuthors: true,
      showNoDeptPubs: true,
      edgeZoomThreshold: 0.4,
    },
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

    document
      .getElementById("global-search-trigger")
      ?.dispatchEvent(new MouseEvent("click", { bubbles: true }));

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

  it("Enter в поле ввода выбирает первый результат", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState({ tab: 2 })); // намеренно не на вкладке автора
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

  it("Enter, нажатый НЕ в поле ввода (например, уже на кнопке результата), не подменяет выбор первым результатом", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    mountGlobalSearch(store, data, NO_PUB_DETAILS, NO_REPO_DETAILS);

    document
      .getElementById("global-search-trigger")
      ?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    const input = document.getElementById("global-search-input") as HTMLInputElement;
    input.value = "П"; // должно найтись больше одного автора
    input.dispatchEvent(new Event("input"));

    const items = [
      ...document.querySelectorAll<HTMLButtonElement>("#global-search-results .tab-list-item"),
    ];
    if (items.length < 2) throw new Error("для этого теста нужно хотя бы два результата");
    const second = items[1];
    if (!second) throw new Error("должен быть второй результат");

    // event.target здесь — сама кнопка (второй результат), не input: код
    // должен проверять именно "event.target === input", а не более широкое
    // "это не INPUT/TEXTAREA" — иначе Enter здесь тоже подхватило бы "выбрать
    // первый" и выбрало бы ПЕРВЫЙ результат вместо второго (или вместо
    // штатного клика по самой кнопке, за который отвечает браузер, а не мы).
    second.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));

    expect(store.get().selection).toBeNull();
  });

  it("Escape закрывает открытое окно", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    mountGlobalSearch(store, data, NO_PUB_DETAILS, NO_REPO_DETAILS);

    document
      .getElementById("global-search-trigger")
      ?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));

    expect((document.getElementById("global-search") as HTMLElement).hidden).toBe(true);
  });

  it("клик по затемнённому фону (не по самому окну) закрывает поиск", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    mountGlobalSearch(store, data, NO_PUB_DETAILS, NO_REPO_DETAILS);

    const overlay = document.getElementById("global-search") as HTMLElement;
    document
      .getElementById("global-search-trigger")
      ?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    overlay.dispatchEvent(new MouseEvent("click", { bubbles: true })); // target === overlay сам по себе

    expect(overlay.hidden).toBe(true);
  });

  it("выбор автора из результатов переключает вкладку на 1 и пишет selection", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState({ tab: 2 })); // намеренно НЕ на вкладке автора
    mountGlobalSearch(store, data, NO_PUB_DETAILS, NO_REPO_DETAILS);

    document
      .getElementById("global-search-trigger")
      ?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
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

    document
      .getElementById("global-search-trigger")
      ?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    const input = document.getElementById("global-search-input") as HTMLInputElement;
    input.value = dept.name;
    input.dispatchEvent(new Event("input"));

    const hit = [
      ...document.querySelectorAll<HTMLButtonElement>("#global-search-results .tab-list-item"),
    ].find((button) => button.dataset.kind === "dept");
    if (!hit) throw new Error(`должен найтись департамент "${dept.name}" среди результатов`);
    hit.click();

    expect(store.get().tab).toBe(3); // не менялась
    expect(store.get().selection).toEqual({ kind: "dept", id: dept.id });
  });

  it("запрос без совпадений показывает 'Ничего не найдено'", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    mountGlobalSearch(store, data, NO_PUB_DETAILS, NO_REPO_DETAILS);

    document
      .getElementById("global-search-trigger")
      ?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    const input = document.getElementById("global-search-input") as HTMLInputElement;
    input.value = "лщывалщыв"; // заведомо не встречается ни в одной подписи фикстуры
    input.dispatchEvent(new Event("input"));

    expect(document.querySelectorAll("#global-search-results .tab-list-item")).toHaveLength(0);
    expect(document.querySelector("#global-search-results .tab-empty")?.textContent).toBe(
      "Ничего не найдено",
    );
  });

  it("пустой запрос (сразу после открытия) показывает департаменты для просмотра, крупнейшие сверху — не 'Ничего не найдено' и не пустой список", async () => {
    const data = await loadSampleGraphData();
    // Департамент 0 во фикстуре (n=7) крупнее департамента 2 (n=5) — 0 должен идти первым.
    const dept0 = data.departments.find((d) => d.id === 0);
    const dept1 = data.departments.find((d) => d.id === 2);
    if (!dept0 || !dept1) throw new Error("фикстура должна содержать департаменты 0 и 2");
    if (dept0.n <= dept1.n)
      throw new Error(
        "фикстура должна давать разброс по размеру департаментов для проверки сортировки",
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

  it("клик по департаменту из подсказки 'для просмотра' выбирает его", async () => {
    const data = await loadSampleGraphData();
    const dept = data.departments[0];
    if (!dept) throw new Error("фикстура должна содержать хотя бы один департамент");
    const store = new Store<AppState>(initialState());
    mountGlobalSearch(store, data, NO_PUB_DETAILS, NO_REPO_DETAILS);

    document
      .getElementById("global-search-trigger")
      ?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    const hit = [
      ...document.querySelectorAll<HTMLButtonElement>("#global-search-results .tab-list-item"),
    ].find((button) => button.textContent === dept.name);
    if (!hit) throw new Error(`департамент "${dept.name}" должен быть в подсказке "для просмотра"`);

    hit.click();

    expect(store.get().selection).toEqual({ kind: "dept", id: dept.id });
    expect((document.getElementById("global-search") as HTMLElement).hidden).toBe(true);
  });
});
