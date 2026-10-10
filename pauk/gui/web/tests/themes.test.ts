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

describe("themes", () => {
  it("all themes have unique ids and the same accent", () => {
    expect(new Set(THEMES.map((theme) => theme.id)).size).toBe(THEMES.length);
    expect(new Set(THEMES.map((theme) => theme.ui.accent)).size).toBe(1);
  });

  it("apply writes CSS variables and color-scheme to the root", () => {
    const root = document.createElement("div");
    themeById("light").apply(root);
    expect(root.style.getPropertyValue("--bg")).toBe(themeById("light").ui.bg);
    expect(root.style.getPropertyValue("--surface-2")).toBe(themeById("light").ui["surface-2"]);
    expect(root.style.colorScheme).toBe("light");
  });

  it("switching themes overwrites all variables of the previous one", () => {
    const root = document.createElement("div");
    themeById("light").apply(root);
    themeById("dark").apply(root);
    for (const [token, value] of Object.entries(themeById("dark").ui)) {
      expect(root.style.getPropertyValue(`--${token}`)).toBe(value);
    }
  });

  it("an unknown id gives the default theme, not a crash", () => {
    expect(themeById("no-such-theme")).toBe(THEMES[0]);
  });
});

describe("mountThemePicker", () => {
  it("one button per theme, labels in the current language, the active one is the current theme", () => {
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

  it("clicking a button switches store.theme and the active button", () => {
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
