import type { GraphData } from "../contracts/graph";
import { parseUrlState, serializeUrlState } from "../core/url";
import type { CameraState } from "sigma/types";
import { t } from "../core/i18n";
import { TAB_LABEL_KEYS } from "./tabs";
import type { AppState, Store, TabId } from "../core/state";

/**
 * Two-way sync of `{screen, tab, selection}` with the address bar. Only a
 * screen or tab change adds a history entry; selection changes replace the
 * current one, so "back" does not step through every click.
 *
 * @returns Unmount function.
 */
export function mountUrlSync(
  store: Store<AppState>,
  data: GraphData,
  navigation?: {
    camera: {
      getState(): CameraState;
      animate(state: CameraState, options: { duration: number }): Promise<void>;
    };
    button: HTMLElement;
  },
): () => void {
  const views = new Map<string, { filters: AppState["filters"]; camera: CameraState }>();
  const session = performance.timeOrigin;
  let sequence = 0;
  const newEntry = () => `${session}/${sequence++}`;
  let entry = newEntry();
  let backTab: TabId | null = null;
  let applyingFromHistory = false;
  let prev = store.get();

  function remember(): void {
    if (navigation && prev.screen === "app") {
      views.set(entry, { filters: prev.filters, camera: navigation.camera.getState() });
    }
  }

  function updateButton(): void {
    if (!navigation) return;
    navigation.button.hidden = backTab === null;
    navigation.button.textContent = backTab === null ? "" : `← ${t(TAB_LABEL_KEYS[backTab], store.get().lang)}`;
  }

  function onPopState(): void {
    remember();
    entry = history.state?.entry ?? newEntry();
    backTab = history.state?.backTab ?? null;
    const view = views.get(entry);
    applyingFromHistory = true;
    try {
      store.set({ ...parseUrlState(location.search, data), ...(view && { filters: view.filters }) });
      // Supersede the selection's automatic camera animation after the graph rebuild.
      if (view) void navigation?.camera.animate(view.camera, { duration: 1 });
    } finally {
      applyingFromHistory = false;
      updateButton();
    }
  }

  function goBack(): void {
    if (backTab !== null) history.back();
  }
  navigation?.button.addEventListener("click", goBack);
  window.addEventListener("popstate", onPopState);
  history.replaceState({ entry, backTab }, "", `?${serializeUrlState(prev)}`);
  updateButton();

  const unsubscribe = store.subscribe((state) => {
    const majorChange = state.screen !== prev.screen || state.tab !== prev.tab;
    if (!applyingFromHistory) {
      const url = `?${serializeUrlState(state)}`;
      if (majorChange) {
        remember();
        backTab = state.screen === "app" && prev.screen === "app" ? prev.tab : null;
        entry = newEntry();
        history.pushState({ entry, backTab }, "", url);
      } else if (state.selection !== prev.selection) {
        history.replaceState({ entry, backTab }, "", url);
      }
    }
    if (majorChange || state.lang !== prev.lang) updateButton();
    prev = state;
  });

  return () => {
    navigation?.button.removeEventListener("click", goBack);
    window.removeEventListener("popstate", onPopState);
    unsubscribe();
  };
}
