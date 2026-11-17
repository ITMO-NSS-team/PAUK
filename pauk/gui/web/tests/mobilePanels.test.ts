import { afterEach, expect, it } from "vitest";
import { Store, type AppState } from "../src/core/state";
import { mountMobilePanels } from "../src/features/mobilePanels";

afterEach(() => {
  document.body.replaceChildren();
});

it("toggles panels, opens selected cards and closes on Escape", () => {
  document.body.innerHTML =
    '<div id="app"><button id="mobile-list"></button><button id="mobile-details"></button><button id="mobile-close"></button><button id="mobile-backdrop"></button></div>';
  const store = new Store<AppState>({
    screen: "app",
    lang: "en",
    selection: null,
    tab: 1,
    theme: "dark",
    filters: {
      minCoauth: 1,
      minSharedAuthors: 1,
      yearMax: 2026,
      showNoDeptAuthors: true,
      showNoDeptPubs: true,
      showExternalAuthors: false,
      showIsolatedAuthors: false,
      edgeZoomThreshold: 0.25,
      showRegions: { 1: true, 2: false, 3: true },
      regionZoomThreshold: 0.4,
      regionMinNodes: 10,
    },
  });
  const unmount = mountMobilePanels(store);
  const app = document.getElementById("app");
  document.getElementById("mobile-list")?.click();
  expect(app?.dataset.mobilePane).toBe("sidebar");
  store.set({ selection: { kind: "node", key: "A1" } });
  expect(app?.dataset.mobilePane).toBe("panel");
  expect(document.getElementById("mobile-details")?.getAttribute("aria-expanded")).toBe("true");
  document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
  expect(app?.dataset.mobilePane).toBe("");
  unmount();
});
