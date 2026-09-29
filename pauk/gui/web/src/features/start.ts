// The menu is shown on every page load, even when the URL points to a node.

import { requireElement } from "../core/dom";
import { t, type LocaleKey } from "../core/i18n";
import type { AppState, Store } from "../core/state";

export type BootStage = "loading" | "rendering" | "error";

const STAGE_LOCALE_KEY: Record<BootStage, LocaleKey> = {
  loading: "start.loading",
  rendering: "start.rendering",
  error: "start.error",
};

// Fixed checkpoints instead of byte progress. "error" keeps the bar where it
// stopped.
const STAGE_PROGRESS: Record<"loading" | "rendering", number> = {
  loading: 15,
  rendering: 70,
};

/** @returns Boot callbacks for app/main.ts. */
export function mountStart(store: Store<AppState>): {
  setBootStage: (stage: BootStage) => void;
  /** What comes next (menu or app) is already decided by `store.screen`. */
  finishBoot: () => void;
  /**
   * Hides the boot screen at once. Call before `showLoadError()`: the boot
   * screen is opaque and sits above `#load-error`.
   */
  hideBootOnError: () => void;
} {
  const boot = requireElement("boot-screen");
  const bar = requireElement("boot-progress-bar");
  const status = requireElement("boot-status");
  const menu = requireElement("menu");
  const app = requireElement("app");
  const brand = requireElement("brand");
  const badge = requireElement("menu-badge-text");
  const title = requireElement("menu-title");
  const subtitle = requireElement("menu-subtitle");
  const enterButton = requireElement("menu-enter");
  const langButtons = menu.querySelectorAll<HTMLButtonElement>("button[data-lang]");

  function setBootStage(stage: BootStage): void {
    if (stage !== "error") bar.style.width = `${STAGE_PROGRESS[stage]}%`;
    status.textContent = t(STAGE_LOCALE_KEY[stage], store.get().lang);
  }

  function finishBoot(): void {
    bar.style.width = "100%";
    // Let the bar reach 100% before it disappears.
    setTimeout(() => {
      boot.hidden = true;
    }, 200);
  }

  function hideBootOnError(): void {
    boot.hidden = true;
  }

  function render(state: AppState): void {
    const isMenu = state.screen === "menu";
    menu.hidden = !isMenu;
    app.hidden = isMenu;

    const lang = state.lang;
    brand.textContent = t("brand.backToMenu", lang);
    badge.textContent = t("start.badge", lang);
    title.textContent = t("start.title", lang);
    subtitle.textContent = t("start.subtitle", lang);
    enterButton.textContent = t("start.cta", lang);
    for (const button of langButtons) {
      button.classList.toggle("menu-lang--active", button.dataset.lang === lang);
    }
  }

  enterButton.addEventListener("click", () => {
    store.set({ screen: "app", tab: 1 });
  });
  for (const button of langButtons) {
    button.addEventListener("click", () => {
      store.set({ lang: button.dataset.lang as AppState["lang"] });
    });
  }
  brand.addEventListener("click", () => {
    store.set({ screen: "menu" });
  });

  render(store.get());
  store.subscribe(render);

  return { setBootStage, finishBoot, hideBootOnError };
}
