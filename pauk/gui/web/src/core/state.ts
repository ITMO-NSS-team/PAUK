import type { NodeKind } from "../contracts/graph";

/** 1 authors, 2 repos, 3 pubs. Internal only, URLs use slugs. */
export type TabId = 1 | 2 | 3;

export const TAB_KIND: Record<TabId, NodeKind> = {
  1: "author",
  2: "repo",
  3: "pub",
};
export const TAB_FOR_KIND: Record<NodeKind, TabId> = Object.fromEntries(
  Object.entries(TAB_KIND).map(([tab, kind]) => [kind, Number(tab) as TabId]),
) as Record<NodeKind, TabId>;

export type Selection =
  | { kind: "node"; key: string }
  | { kind: "edge"; s: string; t: string; w: number }
  | { kind: "dept"; id: number }
  | { kind: "grant"; key: string }
  | null;

/**
 * The menu is an overlay: #app keeps its size underneath, since Sigma is
 * created before the user leaves the menu.
 */
export type Screen = "menu" | "app";

export interface AppState {
  screen: Screen;
  /** Meaningful only when `screen === "app"`. */
  tab: TabId;
  lang: "ru" | "en";
  theme: string;
  selection: Selection;
  /**
   * Defaults live in app/main.ts.
   * - `minSharedAuthors`: minimum shared authors on a pub-pub edge, not the
   *   author count of one pub.
   * - `showExternalAuthors`, `showIsolatedAuthors`: selecting such an author
   *   turns the filter on, see features/filters.ts::mountHiddenAuthorReveal.
   * - `edgeZoomThreshold`: edges hide above this camera ratio.
   * - `regionZoomThreshold`: regions show above this ratio. Its range does not
   *   overlap `edgeZoomThreshold`, so one of the two is always visible.
   * - `regionMinNodes`: smaller region islands are not drawn.
   */
  filters: {
    minCoauth: number;
    minSharedAuthors: number;
    yearMax: number;
    showNoDeptAuthors: boolean;
    showNoDeptPubs: boolean;
    showExternalAuthors: boolean;
    showIsolatedAuthors: boolean;
    edgeZoomThreshold: number;
    showRegions: Record<TabId, boolean>;
    regionZoomThreshold: number;
    regionMinNodes: number;
  };
}

/**
 * Regions are on for the tab and the camera is past the zoom threshold. Then
 * hover and click act on regions, not on nodes and edges.
 */
export function isRegionMode(state: Readonly<AppState>, cameraRatio: number): boolean {
  return state.filters.showRegions[state.tab] && cameraRatio > state.filters.regionZoomThreshold;
}

/** Minimal observable store, no actions or reducers. */
export class Store<S extends object> {
  private state: S;
  private listeners = new Set<(state: Readonly<S>) => void>();

  constructor(initial: S) {
    this.state = initial;
  }

  get(): Readonly<S> {
    return this.state;
  }

  /** Shallow merge, then notifies listeners synchronously. */
  set(patch: Partial<S>): void {
    this.state = { ...this.state, ...patch };
    this.notify();
  }

  /**
   * For data kept outside the state, e.g. detail maps filled in place after a
   * lazy load.
   */
  notify(): void {
    // Read this.state per listener: a listener may call set() and start a
    // nested round, and the rest must not overwrite its result with a stale
    // snapshot. One failing listener must not stop the others.
    this.listeners.forEach((listener) => {
      try {
        listener(this.state);
      } catch (error: unknown) {
        console.error("[store] a subscriber threw while receiving state:", error);
      }
    });
  }

  subscribe(listener: (state: Readonly<S>) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }
}
