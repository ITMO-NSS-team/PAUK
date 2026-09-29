// Pure {screen, tab, selection} <-> query string, no DOM. Language and
// filters are personal view settings and stay out of shared links.

import type { Edge, GraphData } from "../contracts/graph";
import { groupsById, indexByKey } from "./data";
import { TAB_KIND, type Screen, type Selection, type TabId } from "./state";

const TAB_SLUGS: Record<TabId, string> = {
  1: "persons",
  2: "repos",
  3: "pubs",
};

const SLUG_TO_TAB: Record<string, TabId> = Object.fromEntries(
  Object.entries(TAB_SLUGS).map(([id, slug]) => [slug, Number(id) as TabId]),
);

const MENU_SLUG = "menu";

function tabEdges(data: GraphData, tab: TabId): Edge[] {
  switch (tab) {
    case 1:
      return data.coauth_edges;
    case 2:
      return data.repo_edges;
    case 3:
      return data.pub_edges;
  }
}

/** Edge weight is not written: parsing takes it from `data`. */
export function serializeUrlState(state: { screen: Screen; tab: TabId; selection: Selection }): string {
  if (state.screen === "menu") return new URLSearchParams({ tab: MENU_SLUG }).toString();

  const params = new URLSearchParams({ tab: TAB_SLUGS[state.tab] });
  const selection = state.selection;

  if (selection?.kind === "node") {
    params.set("sel", "node");
    params.set("key", selection.key);
  } else if (selection?.kind === "edge") {
    params.set("sel", "edge");
    params.set("s", selection.s);
    params.set("t", selection.t);
  } else if (selection?.kind === "dept") {
    params.set("sel", "dept");
    params.set("id", String(selection.id));
  } else if (selection?.kind === "grant") {
    params.set("sel", "grant");
    params.set("key", selection.key);
  }

  return params.toString();
}

/**
 * Unknown or missing `tab` opens the menu. A selection missing from `data`
 * (stale link) becomes `null`. Edge endpoints match in either order.
 */
export function parseUrlState(search: string, data: GraphData): { screen: Screen; tab: TabId; selection: Selection } {
  const params = new URLSearchParams(search);
  const rawTab = params.get("tab");
  const tab = rawTab !== null ? SLUG_TO_TAB[rawTab] : undefined;

  if (rawTab === null || rawTab === MENU_SLUG || tab === undefined) {
    return { screen: "menu", tab: 1, selection: null };
  }

  const kind = params.get("sel");
  if (kind === "node") {
    const key = params.get("key");
    const node = key !== null ? indexByKey(data).get(key) : undefined;
    // The node must belong to the tab being drawn.
    if (key !== null && node && node.kind === TAB_KIND[tab]) {
      return { screen: "app", tab, selection: { kind: "node", key } };
    }
  } else if (kind === "edge") {
    const s = params.get("s");
    const t = params.get("t");
    if (s !== null && t !== null) {
      const edge = tabEdges(data, tab).find((e) => (e.s === s && e.t === t) || (e.s === t && e.t === s));
      if (edge) return { screen: "app", tab, selection: { kind: "edge", s: edge.s, t: edge.t, w: edge.w } };
    }
  } else if (kind === "grant") {
    // Grants live in pubs-detail.json, loaded after the URL is parsed, so the
    // key cannot be checked here.
    const key = params.get("key");
    if (key) return { screen: "app", tab, selection: { kind: "grant", key } };
  } else if (kind === "dept") {
    const id = Number(params.get("id"));
    if (groupsById(data).has(id)) return { screen: "app", tab, selection: { kind: "dept", id } };
  }

  return { screen: "app", tab, selection: null };
}
