import type { GraphData } from "../contracts/graph";
import { parseUrlState, serializeUrlState } from "../core/url";
import type { AppState, Store } from "../core/state";

/**
 * Two-way sync of `{screen, tab, selection}` with the address bar. Only a
 * screen or tab change adds a history entry; selection changes replace the
 * current one, so "back" does not step through every click.
 *
 * @returns Unmount function.
 */
export function mountUrlSync(store: Store<AppState>, data: GraphData): () => void {
  // Stops popstate from writing the state it just read back into history.
  let applyingFromHistory = false;

  function onPopState(): void {
    applyingFromHistory = true;
    store.set(parseUrlState(location.search, data));
    applyingFromHistory = false;
  }

  window.addEventListener("popstate", onPopState);

  history.replaceState(null, "", `?${serializeUrlState(store.get())}`);

  let prev = store.get();
  const unsubscribe = store.subscribe((state) => {
    if (state.screen === prev.screen && state.tab === prev.tab && state.selection === prev.selection) return;
    const majorChange = state.screen !== prev.screen || state.tab !== prev.tab;
    prev = state;
    if (applyingFromHistory) return;

    const url = `?${serializeUrlState(state)}`;
    if (majorChange) history.pushState(null, "", url);
    else history.replaceState(null, "", url);
  });

  return () => {
    window.removeEventListener("popstate", onPopState);
    unsubscribe();
  };
}
