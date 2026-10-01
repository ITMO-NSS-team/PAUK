// GraphData -> graphology graph for Sigma. Each tab draws its own graph:
// authors, repos or pubs, never all at once.

import type Graph from "graphology";
import type Sigma from "sigma";
import type { EdgeDisplayData, NodeDisplayData } from "sigma/types";
import type { AuthorNode, Edge, GraphData, PubDetail, PubNode, RepoNode } from "../contracts/graph";
import { MAP_CONFIG, NO_DEPT_COLOR } from "../core/config";
import { grantIndex, groupIdOf, groupsById, nodeLabel } from "../core/data";
import { localize, type Lang } from "../core/i18n";
import { themeById } from "../core/themes";
import { isRegionMode, type AppState, type Selection, type Store, type TabId } from "../core/state";

type GraphNode = AuthorNode | RepoNode | PubNode;
type Filters = AppState["filters"];
type PubDetailsByKey = Map<string, PubDetail>;

const DEPT_NODE_PREFIX = "dept:";

/**
 * Key of an invisible department anchor node (size 0), used for camera
 * targets and department neighbourhood.
 */
export function deptNodeKey(deptId: number): string {
  return `${DEPT_NODE_PREFIX}${deptId}`;
}

/** @returns Department id for an anchor key, otherwise `null`. */
export function parseDeptNodeKey(key: string): number | null {
  if (!key.startsWith(DEPT_NODE_PREFIX)) return null;
  const id = Number(key.slice(DEPT_NODE_PREFIX.length));
  return Number.isNaN(id) ? null : id;
}

function truncateLabel(label: string, maxLength: number): string {
  return label.length > maxLength ? `${label.slice(0, maxLength - 1)}…` : label;
}

/**
 * Found by color, not by the localized name. `null` when absent (e.g. test
 * fixtures): the "no department" filter then hides nothing.
 */
export function noDeptId(data: GraphData): number | null {
  return data.departments.find((dept) => dept.color === NO_DEPT_COLOR)?.id ?? null;
}

const isolatedByData = new WeakMap<GraphData, Map<boolean, Set<string>>>();

/**
 * At most one pub, no co-authors and no repos. External co-authors count only
 * when `withExternal` is set.
 */
export function isolatedAuthors(data: GraphData, withExternal: boolean): Set<string> {
  const byMode = isolatedByData.get(data) ?? new Map<boolean, Set<string>>();
  isolatedByData.set(data, byMode);
  const cached = byMode.get(withExternal);
  if (cached) return cached;

  const counted = new Set(
    data.authors.filter((a) => withExternal || a.is_itmo !== false).map((a) => a.key),
  );
  const withRepo = new Set(data.repo_author_edges.map((edge) => edge.t));
  const pubAuthors = new Map<string, string[]>();
  for (const { s, t } of data.all_edges) {
    if (counted.has(s)) pubAuthors.set(t, [...(pubAuthors.get(t) ?? []), s]);
  }
  const withCoauthor = new Set<string>();
  for (const authors of pubAuthors.values()) {
    if (authors.length > 1) authors.forEach((key) => withCoauthor.add(key));
  }

  const isolated = new Set(
    data.authors
      .filter(
        (a) =>
          counted.has(a.key) &&
          a.pubs_count <= 1 &&
          !withRepo.has(a.key) &&
          !withCoauthor.has(a.key),
      )
      .map((a) => a.key),
  );
  byMode.set(withExternal, isolated);
  return isolated;
}

/** Shared by the map, the tab list and search. */
export function isAuthorShown(data: GraphData, author: AuthorNode, filters: Filters): boolean {
  if (author.is_itmo === false && !filters.showExternalAuthors) return false;
  return (
    filters.showIsolatedAuthors ||
    !isolatedAuthors(data, filters.showExternalAuthors).has(author.key)
  );
}

export function visibleAuthors(data: GraphData, filters: Filters): AuthorNode[] {
  return data.authors.filter((author) => isAuthorShown(data, author, filters));
}

/** Pubs with an unknown year are never hidden by `yearMax`. */
export function tabGraphNodes(data: GraphData, tab: TabId, filters: Filters): GraphNode[] {
  const excludedDept = noDeptId(data);
  switch (tab) {
    case 1: {
      const authors = visibleAuthors(data, filters);
      return filters.showNoDeptAuthors ? authors : authors.filter((a) => a.dept !== excludedDept);
    }
    case 2:
      return data.repos;
    case 3: {
      const pubs = data.pubs.filter((pub) => pub.year === null || pub.year <= filters.yearMax);
      return filters.showNoDeptPubs ? pubs : pubs.filter((pub) => pub.dept !== excludedDept);
    }
  }
}

/** Repos have no weight threshold. */
function tabGraphEdges(data: GraphData, tab: TabId, filters: Filters): Edge[] {
  switch (tab) {
    case 1:
      return data.coauth_edges.filter((edge) => edge.w >= filters.minCoauth);
    case 2:
      return data.repo_edges;
    case 3:
      return data.pub_edges.filter((edge) => edge.w >= filters.minSharedAuthors);
  }
}

export function populateGraph(
  graph: Graph,
  data: GraphData,
  lang: Lang,
  tab: TabId,
  filters: Filters,
  pubDetails: PubDetailsByKey,
): void {
  graph.clear();

  const groups = groupsById(data);

  for (const node of tabGraphNodes(data, tab, filters)) {
    graph.addNode(node.key, {
      x: node.gx,
      y: node.gy,
      dept: groupIdOf(node),
      size: MAP_CONFIG.node.radius,
      color: groups.get(groupIdOf(node))?.color ?? MAP_CONFIG.node.fallbackColor,
      label: truncateLabel(nodeLabel(node, lang, pubDetails), MAP_CONFIG.node.labelMaxLength),
    });
  }

  // hasNode() already reflects the node filters above.
  for (const edge of tabGraphEdges(data, tab, filters)) {
    if (!graph.hasNode(edge.s) || !graph.hasNode(edge.t)) continue;
    // mergeEdge: addEdge throws on a repeated pair.
    graph.mergeEdge(edge.s, edge.t, {
      size: MAP_CONFIG.edge.width,
      weight: edge.w,
    });
  }

  addDeptLabelAnchors(graph, data, tab, filters, lang);
}

/**
 * One invisible anchor per department present in the tab, at the centroid of
 * its nodes in this tab's layout. Department edges are added hidden, only so
 * `areNeighbors()` works for department selection.
 */
function addDeptLabelAnchors(
  graph: Graph,
  data: GraphData,
  tab: TabId,
  filters: Filters,
  lang: Lang,
): void {
  const nodesByDept = new Map<number, GraphNode[]>();
  for (const node of tabGraphNodes(data, tab, filters)) {
    const list = nodesByDept.get(groupIdOf(node)) ?? [];
    list.push(node);
    nodesByDept.set(groupIdOf(node), list);
  }

  const deptById = groupsById(data);

  for (const [deptId, nodes] of nodesByDept) {
    const dept = deptById.get(deptId);
    if (!dept) continue;

    const cx = nodes.reduce((sum, node) => sum + node.gx, 0) / nodes.length;
    const cy = nodes.reduce((sum, node) => sum + node.gy, 0) / nodes.length;

    graph.addNode(deptNodeKey(deptId), {
      x: cx,
      y: cy,
      size: 0,
      color: dept.color,
      label: localize(dept.name, dept.name_en, lang),
    });
  }

  for (const edge of data.dept_edges) {
    if (!graph.hasNode(deptNodeKey(edge.s)) || !graph.hasNode(deptNodeKey(edge.t))) continue;
    graph.mergeEdge(deptNodeKey(edge.s), deptNodeKey(edge.t), {
      size: MAP_CONFIG.edge.width,
      weight: edge.w,
    });
  }
}

/**
 * The single node/edge reducer pair: `setSetting` replaces, so two separate
 * reducers would overwrite each other.
 *
 * Selection and hover are independent focus sources: hovering another node
 * does not cancel the selection, and a selection does not block hover
 * previews. Only the selected node grows. Neighbours of the selection, the
 * endpoints of a selected edge and their neighbours get forced labels.
 * Everything else is dimmed, unrelated edges are hidden. Edges also hide above
 * the `edgeZoomThreshold` camera ratio.
 *
 * @returns Setters for hover and camera ratio, called from mountReactiveGraph.
 */
function applyGraphStyling(
  renderer: Sigma,
  store: Store<AppState>,
  pubDetails: PubDetailsByKey,
): { setHoveredNode: (key: string | null) => void; setCameraRatio: (ratio: number) => void } {
  const graph = renderer.getGraph();
  let hoveredNode: string | null = null;
  let cameraRatio = 1;

  function selectionFocusKey(): string | null {
    const selection = store.get().selection;
    return selection?.kind === "node" ? selection.key : null;
  }

  function selectedDept(): number | null {
    const selection = store.get().selection;
    return selection?.kind === "dept" ? selection.id : null;
  }

  function inSelectedDept(nodeKey: string): boolean {
    const dept = selectedDept();
    return (
      dept !== null && graph.hasNode(nodeKey) && graph.getNodeAttribute(nodeKey, "dept") === dept
    );
  }

  function inSelectedGrant(nodeKey: string): boolean {
    const selection = store.get().selection;
    return (
      selection?.kind === "grant" &&
      (grantIndex(pubDetails).get(selection.key)?.pubs.includes(nodeKey) ?? false)
    );
  }

  function selectionEdgeEndpoints(): [string, string] | null {
    const selection = store.get().selection;
    return selection?.kind === "edge" ? [selection.s, selection.t] : null;
  }

  /** `areNeighbors()` throws on a missing node, so check existence first. */
  function isNeighborOf(focus: string | null, nodeKey: string): boolean {
    return (
      focus !== null &&
      focus !== nodeKey &&
      graph.hasNode(focus) &&
      graph.areNeighbors(focus, nodeKey)
    );
  }

  renderer.setSetting("nodeReducer", (nodeKey, data): Partial<NodeDisplayData> => {
    // Anchors are never drawn; regions draw department names.
    if (parseDeptNodeKey(nodeKey) !== null) {
      return { ...data, hidden: true, label: "", forceLabel: false, highlighted: false };
    }
    const res: Partial<NodeDisplayData> = { ...data };
    const selection = store.get().selection;
    const isSelected = selection?.kind === "node" && selection.key === nodeKey;
    const edgeEndpoints = selectionEdgeEndpoints();
    const isSelectedEdgeEndpoint = edgeEndpoints !== null && edgeEndpoints.includes(nodeKey);

    if (isSelected) {
      res.highlighted = true;
      res.size = MAP_CONFIG.node.radiusSelected;
    }

    const selKey = selectionFocusKey();
    const isNeighborOfSelection = isNeighborOf(selKey, nodeKey);
    const isHoveredNode = hoveredNode !== null && hoveredNode === nodeKey;
    const isNeighborOfHover = isNeighborOf(hoveredNode, nodeKey);
    // Neighbours of either endpoint of the selected edge stay bright, since
    // their edges stay visible.
    const isNeighborOfEdgeSelection =
      edgeEndpoints !== null &&
      (isNeighborOf(edgeEndpoints[0], nodeKey) || isNeighborOf(edgeEndpoints[1], nodeKey));
    // Global: any focus dims every other node.
    const anyFocusActive =
      selKey !== null ||
      hoveredNode !== null ||
      selection?.kind === "edge" ||
      selection?.kind === "dept" ||
      selection?.kind === "grant";

    if (!isSelected && anyFocusActive) {
      if (
        isNeighborOfSelection ||
        isHoveredNode ||
        isNeighborOfHover ||
        isSelectedEdgeEndpoint ||
        isNeighborOfEdgeSelection ||
        inSelectedDept(nodeKey) ||
        inSelectedGrant(nodeKey)
      ) {
        // No forced labels for hover neighbours: too noisy while moving the mouse.
        if (isNeighborOfSelection || isSelectedEdgeEndpoint || inSelectedGrant(nodeKey))
          res.forceLabel = true;
      } else {
        res.color = themeById(store.get().theme).map.dimNode;
        res.label = "";
      }
    }

    return res;
  });

  renderer.setSetting("edgeReducer", (edgeKey, data): Partial<EdgeDisplayData> => {
    const [s, t] = graph.extremities(edgeKey);
    if (parseDeptNodeKey(s) !== null) return { ...data, hidden: true };

    if (cameraRatio > store.get().filters.edgeZoomThreshold) return { ...data, hidden: true };

    const selection = store.get().selection;
    const isSelectedEdge =
      selection?.kind === "edge" &&
      ((s === selection.s && t === selection.t) || (s === selection.t && t === selection.s));

    if (isSelectedEdge) {
      return {
        ...data,
        color: themeById(store.get().theme).map.edgeSelected,
        size: MAP_CONFIG.edge.widthSelected,
      };
    }

    // A selected edge also shows all other edges of its endpoints.
    const selKey = selectionFocusKey();
    const edgeEndpoints = selectionEdgeEndpoints();
    const touchesSelection = selKey !== null && (s === selKey || t === selKey);
    const touchesEdgeSelectionEndpoint =
      edgeEndpoints !== null && (edgeEndpoints.includes(s) || edgeEndpoints.includes(t));
    const touchesHover = hoveredNode !== null && (s === hoveredNode || t === hoveredNode);
    // A selected department has no node whose edges could be highlighted.
    const anyFocusActive =
      selKey !== null ||
      hoveredNode !== null ||
      edgeEndpoints !== null ||
      selectedDept() !== null ||
      store.get().selection?.kind === "grant";
    if (anyFocusActive && !touchesSelection && !touchesEdgeSelectionEndpoint && !touchesHover) {
      return { ...data, hidden: true };
    }

    return { ...data, color: themeById(store.get().theme).map.edge };
  });

  return {
    setHoveredNode(key) {
      if (hoveredNode === key) return;
      hoveredNode = key;
      renderer.refresh();
    },
    setCameraRatio(ratio) {
      if (cameraRatio === ratio) return;
      cameraRatio = ratio;
      // Zoomed out into region mode with the cursor on a node.
      if (isRegionMode(store.get(), ratio)) hoveredNode = null;
      renderer.refresh();
    },
  };
}

/** Used after a rebuild to drop a selection the new graph no longer has. */
function selectionExistsIn(graph: Graph, selection: Selection): boolean {
  if (selection === null) return true;
  if (selection.kind === "node") return graph.hasNode(selection.key);
  if (selection.kind === "dept") return graph.hasNode(deptNodeKey(selection.id));
  if (selection.kind === "grant") return true;
  // mergeEdge on a mixed graph creates directed edges, and hasEdge checks
  // one direction only.
  return graph.hasEdge(selection.s, selection.t) || graph.hasEdge(selection.t, selection.s);
}

/**
 * Reads coordinates from `getNodeDisplayData`, not graph attributes: the
 * camera lives in Sigma's normalized space, raw layout coordinates send it
 * off screen. Edges do not move the camera.
 */
function flyToSelection(renderer: Sigma, selection: Selection): void {
  if (selection === null || selection.kind === "edge" || selection.kind === "grant") return;

  const key = selection.kind === "node" ? selection.key : deptNodeKey(selection.id);
  const nodeData = renderer.getNodeDisplayData(key);
  if (!nodeData) return;

  // Departments pan without zooming in: regions disappear up close.
  const target =
    selection.kind === "node"
      ? { x: nodeData.x, y: nodeData.y, ratio: MAP_CONFIG.camera.focusRatio }
      : { x: nodeData.x, y: nodeData.y };
  renderer.getCamera().animate(target, {
    duration: MAP_CONFIG.camera.focusDuration,
    easing: "quadraticInOut",
  });
}

/**
 * Rebuilds the graph on tab, language or filter change and restyles it on
 * selection change. The renderer is passed in so tests can use a fake: Sigma
 * needs WebGL, which jsdom lacks.
 *
 * @returns Unmount function.
 */
export function mountReactiveGraph(
  renderer: Sigma,
  store: Store<AppState>,
  data: GraphData,
  pubDetails: PubDetailsByKey,
): () => void {
  const graph = renderer.getGraph();
  const { setHoveredNode, setCameraRatio } = applyGraphStyling(renderer, store, pubDetails);

  function onEnterNode({ node }: { node: string }): void {
    // In region mode the region under the cursor is highlighted instead.
    if (isRegionMode(store.get(), renderer.getCamera().getState().ratio)) return;
    setHoveredNode(node);
  }
  function onLeaveNode(): void {
    setHoveredNode(null);
  }
  renderer.on("enterNode", onEnterNode);
  renderer.on("leaveNode", onLeaveNode);

  // Read now, not only on the first camera move, so the initial mode is right.
  const camera = renderer.getCamera();
  function onCameraUpdated(state: { ratio: number }): void {
    setCameraRatio(state.ratio);
  }
  camera.on("updated", onCameraUpdated);
  setCameraRatio(camera.getState().ratio);

  let prev = store.get();
  const unsubscribe = store.subscribe((state) => {
    // Tab and selection can change in one patch (global search), so both
    // checks below run.
    const { tab: prevTab, lang: prevLang, filters: prevFilters, selection: prevSelection } = prev;
    prev = state;

    if (state.tab !== prevTab || state.lang !== prevLang || state.filters !== prevFilters) {
      populateGraph(graph, data, state.lang, state.tab, state.filters, pubDetails);
      // The old selection may not exist in the rebuilt graph, and
      // areNeighbors() would throw on it and freeze the render loop.
      if (!selectionExistsIn(graph, state.selection)) {
        store.set({ selection: null });
        return; // the nested notify() already handled it
      }
    }

    if (state.selection !== prevSelection) {
      renderer.refresh();
      flyToSelection(renderer, state.selection);
    }
  });

  return () => {
    renderer.off("enterNode", onEnterNode);
    renderer.off("leaveNode", onLeaveNode);
    camera.off("updated", onCameraUpdated);
    unsubscribe();
  };
}

/** Debug readout of `camera.ratio` for tuning zoom thresholds. */
export function mountZoomDebug(renderer: Sigma): () => void {
  const el = document.createElement("div");
  Object.assign(el.style, {
    position: "absolute",
    bottom: "12px",
    left: "12px",
    // Below #menu (z-index 26), which must cover it.
    zIndex: "5",
    padding: "4px 8px",
    background: "rgba(0, 0, 0, 0.6)",
    color: "#fff",
    fontSize: "11px",
    fontFamily: "monospace",
    borderRadius: "4px",
    pointerEvents: "none",
  });
  renderer.getContainer().appendChild(el);

  const camera = renderer.getCamera();
  function render(): void {
    el.textContent = `zoom ratio: ${camera.getState().ratio.toFixed(3)}`;
  }
  camera.on("updated", render);
  render();

  return () => {
    camera.off("updated", render);
    el.remove();
  };
}
