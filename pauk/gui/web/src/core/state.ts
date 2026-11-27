import type { NodeKind } from "../contracts/graph";

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
  expandSelection?: boolean;
  screen: Screen;
  tab: TabId;
  lang: "ru" | "en";
  theme: string;
  selection: Selection;
  filters: {
    minCoauth: number;
    minSharedAuthors: number;
    yearMax: number;
    showNoDeptAuthors: boolean;
    showNoDeptPubs: boolean;
    showExternalAuthors: boolean;
    showIsolatedAuthors: boolean;
    edgeZoomThreshold: number;
    labelZoomThreshold?: number;
    showRegions: Record<TabId, boolean>;
    regionZoomThreshold: number;
    regionMinNodes: number;
  };
}

export function isRegionMode(state: Readonly<AppState>, cameraRatio: number): boolean {
  return state.filters.showRegions[state.tab] && cameraRatio > state.filters.regionZoomThreshold;
}

export class Store<S extends object> {
  private state: S;
  private listeners = new Set<(state: Readonly<S>) => void>();

  constructor(initial: S) {
    this.state = initial;
  }

  get(): Readonly<S> {
    return this.state;
  }

  set(patch: Partial<S>): void {
    this.state = { ...this.state, ...patch };
    this.notify();
  }

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
