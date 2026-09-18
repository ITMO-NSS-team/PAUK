import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Store, type AppState } from "../src/core/state";
import { mountStart } from "../src/features/start";

function initialState(overrides: Partial<AppState> = {}): AppState {
  return {
    screen: "menu",
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

/** Минимальная разметка — ровно те id, которые requireElement() ищет внутри mountStart(). */
function mountMarkup(): void {
  document.body.innerHTML = `
    <div id="boot-screen">
      <div class="boot-progress-bar" id="boot-progress-bar"></div>
      <div class="boot-status" id="boot-status"></div>
    </div>
    <div id="menu" hidden>
      <span id="menu-badge-text"></span>
      <h1 id="menu-title"></h1>
      <p id="menu-subtitle"></p>
      <button type="button" id="menu-enter"></button>
      <div>
        <button type="button" data-lang="ru"></button>
        <button type="button" data-lang="en"></button>
      </div>
    </div>
    <div id="app" hidden>
      <button type="button" id="brand"></button>
    </div>
  `;
}

describe("mountStart", () => {
  beforeEach(() => {
    mountMarkup();
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("setBootStage обновляет ширину прогресс-бара и статус-текст", () => {
    const store = new Store<AppState>(initialState());
    const { setBootStage } = mountStart(store);

    setBootStage("loading");
    const bar = document.getElementById("boot-progress-bar") as HTMLElement;
    expect(bar.style.width).toBe("15%");
    expect(document.getElementById("boot-status")?.textContent).toBe("Загрузка данных…");

    setBootStage("rendering");
    expect(bar.style.width).toBe("70%");
    expect(document.getElementById("boot-status")?.textContent).toBe("Отрисовка графа…");
  });

  it("setBootStage('error') не двигает бар — только меняет статус-текст", () => {
    const store = new Store<AppState>(initialState());
    const { setBootStage } = mountStart(store);
    const bar = document.getElementById("boot-progress-bar") as HTMLElement;

    setBootStage("rendering");
    setBootStage("error");

    expect(bar.style.width).toBe("70%"); // не сдвинулся
    expect(document.getElementById("boot-status")?.textContent).toBe("Ошибка загрузки.");
  });

  it("hideBootOnError прячет boot-экран немедленно, без задержки finishBoot и без доводки бара до 100%", () => {
    const store = new Store<AppState>(initialState());
    const { setBootStage, hideBootOnError } = mountStart(store);
    const boot = document.getElementById("boot-screen") as HTMLElement;
    const bar = document.getElementById("boot-progress-bar") as HTMLElement;

    setBootStage("rendering");
    hideBootOnError();

    // Синхронно, а не после setTimeout, как finishBoot() — баннер с точной
    // причиной ошибки (#load-error) стоит ниже boot-экрана по z-index и
    // должен стать видимым сразу, а не через 200мс.
    expect(boot.hidden).toBe(true);
    expect(bar.style.width).toBe("70%"); // не "доскакивает" до 100%, в отличие от finishBoot()
  });

  it("finishBoot прячет boot-экран, видимость меню/приложения остаётся под управлением screen", () => {
    const store = new Store<AppState>(initialState());
    const { finishBoot } = mountStart(store);

    finishBoot();
    vi.advanceTimersByTime(200);

    expect((document.getElementById("boot-screen") as HTMLElement).hidden).toBe(true);
  });

  it("screen: 'menu' — показано меню, приложение скрыто", () => {
    const store = new Store<AppState>(initialState({ screen: "menu" }));
    mountStart(store);

    expect((document.getElementById("menu") as HTMLElement).hidden).toBe(false);
    expect((document.getElementById("app") as HTMLElement).hidden).toBe(true);
  });

  it("screen: 'app' — показано приложение, меню скрыто", () => {
    const store = new Store<AppState>(initialState({ screen: "app" }));
    mountStart(store);

    expect((document.getElementById("menu") as HTMLElement).hidden).toBe(true);
    expect((document.getElementById("app") as HTMLElement).hidden).toBe(false);
  });

  it("клик по кнопке входа переключает screen на 'app', всегда на первую вкладку", () => {
    const store = new Store<AppState>(initialState());
    mountStart(store);

    document.getElementById("menu-enter")?.click();

    expect(store.get().screen).toBe("app");
    expect(store.get().tab).toBe(1);
  });

  it("клик по кнопке входа не трогает selection сам по себе — обнулять устаревший выбор при смене вкладки умеет map/build.ts::mountReactiveGraph (см. tests/build.test.ts)", () => {
    const store = new Store<AppState>(
      initialState({ screen: "menu", tab: 2, selection: { kind: "node", key: "R1" } }),
    );
    mountStart(store);

    document.getElementById("menu-enter")?.click();

    expect(store.get().selection).toEqual({ kind: "node", key: "R1" });
  });

  it("клик по кнопке языка в меню переключает store.lang", () => {
    const store = new Store<AppState>(initialState({ lang: "ru" }));
    mountStart(store);

    document.querySelector<HTMLButtonElement>('[data-lang="en"]')?.click();

    expect(store.get().lang).toBe("en");
  });

  it("клик по #brand возвращает в меню", () => {
    const store = new Store<AppState>(initialState({ screen: "app" }));
    mountStart(store);

    document.getElementById("brand")?.click();

    expect(store.get().screen).toBe("menu");
  });

  it("смена языка перерисовывает текст меню", () => {
    const store = new Store<AppState>(initialState());
    mountStart(store);

    store.set({ lang: "en" });

    expect(document.getElementById("menu-title")?.textContent).toBe(
      "ITMO co-authorship and open-source code map",
    );
  });

  it("#brand показывает «← Меню»/«← Menu» под текущий язык — не название проекта", () => {
    const store = new Store<AppState>(initialState({ lang: "ru" }));
    mountStart(store);

    expect(document.getElementById("brand")?.textContent).toBe("← Меню");

    store.set({ lang: "en" });
    expect(document.getElementById("brand")?.textContent).toBe("← Menu");
  });
});
