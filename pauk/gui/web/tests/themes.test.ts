import { describe, expect, it } from "vitest";
import { Store, type AppState } from "../src/core/state";
import { THEMES, themeById } from "../src/core/themes";
import { mountThemePicker } from "../src/features/themePicker";

function initialState(overrides: Partial<AppState> = {}): AppState {
  return {
    screen: "menu",
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

describe("темы", () => {
  it("у всех тем уникальные id и один и тот же акцент", () => {
    expect(new Set(THEMES.map((theme) => theme.id)).size).toBe(THEMES.length);
    expect(new Set(THEMES.map((theme) => theme.ui.accent)).size).toBe(1);
  });

  it("apply пишет CSS-переменные и color-scheme на корень", () => {
    const root = document.createElement("div");
    themeById("light").apply(root);
    expect(root.style.getPropertyValue("--bg")).toBe(themeById("light").ui.bg);
    expect(root.style.getPropertyValue("--surface-2")).toBe(themeById("light").ui["surface-2"]);
    expect(root.style.colorScheme).toBe("light");
  });

  it("смена темы перезаписывает все переменные прошлой", () => {
    const root = document.createElement("div");
    themeById("light").apply(root);
    themeById("dark").apply(root);
    for (const [token, value] of Object.entries(themeById("dark").ui)) {
      expect(root.style.getPropertyValue(`--${token}`)).toBe(value);
    }
  });

  it("неизвестный id — тема по умолчанию, а не падение", () => {
    expect(themeById("no-such-theme")).toBe(THEMES[0]);
  });
});

describe("mountThemePicker", () => {
  it("по кнопке на тему, подписи на текущем языке, активная — текущая тема", () => {
    document.body.innerHTML = `<div id="menu-theme"></div>`;
    const store = new Store(initialState());
    mountThemePicker(store);

    const buttons = [...document.querySelectorAll<HTMLButtonElement>("#menu-theme button")];
    expect(buttons.map((b) => b.textContent)).toEqual(THEMES.map((theme) => theme.name.ru));
    expect(
      buttons.filter((b) => b.classList.contains("menu-lang--active")).map((b) => b.dataset.theme),
    ).toEqual(["dark"]);

    store.set({ lang: "en" });
    expect(buttons.map((b) => b.textContent)).toEqual(THEMES.map((theme) => theme.name.en));
  });

  it("клик по кнопке переключает store.theme и активную кнопку", () => {
    document.body.innerHTML = `<div id="menu-theme"></div>`;
    const store = new Store(initialState());
    mountThemePicker(store);

    document.querySelector<HTMLButtonElement>('#menu-theme button[data-theme="light"]')?.click();

    expect(store.get().theme).toBe("light");
    expect(
      document
        .querySelector('#menu-theme button[data-theme="light"]')
        ?.classList.contains("menu-lang--active"),
    ).toBe(true);
    expect(
      document
        .querySelector('#menu-theme button[data-theme="dark"]')
        ?.classList.contains("menu-lang--active"),
    ).toBe(false);
  });
});
