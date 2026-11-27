import Graph from "graphology";
import Sigma from "sigma";
import type { Settings } from "sigma/settings";
import type { NodeDisplayData, PartialButFor } from "sigma/types";
import type { AuthorDetail, GraphData, PubDetail, RepoDetail } from "../contracts/graph";
import { DATA_CONFIG, FILTER_CONFIG, MAP_CONFIG } from "../core/config";
import { loadGraphData } from "../core/data";
import { DetailLoader } from "../core/details";
import { requireElement, showLoadError } from "../core/dom";
import { t } from "../core/i18n";
import { loggedStep } from "../core/log";
import { isRegionMode, Store, type AppState } from "../core/state";
import { themeById } from "../core/themes";
import { mountFilters, mountHiddenAuthorReveal } from "../features/filters";
import { mountDetailErrors } from "../features/detailErrors";
import { mountPanelToggles } from "../features/panelToggles";
import { mountMobilePanels } from "../features/mobilePanels";
import { mountGlobalSearch } from "../features/globalSearch";
import { mountPanel } from "../features/panels";
import { mountZoomControls } from "../features/zoomControls";
import { mountSelection } from "../features/selection";
import { mountStart } from "../features/start";
import { mountTabs } from "../features/tabs";
import { mountThemePicker } from "../features/themePicker";
import { mountUrlSync } from "../features/urlSync";
import { mountNodeProximity } from "../map/proximity";
import { mountReactiveGraph, populateGraph } from "../map/build";
import { mountRegions } from "../map/regions";

type LabelNode = PartialButFor<NodeDisplayData, "x" | "y" | "size" | "label" | "color">;

function drawNodeLabels(
  context: CanvasRenderingContext2D,
  nodes: LabelNode[],
  settings: Settings,
): void {
  context.font = `${settings.labelWeight} ${settings.labelSize}px ${settings.labelFont}`;
  context.lineJoin = "round";
  context.lineWidth = MAP_CONFIG.node.labelHaloWidth;
  context.strokeStyle = themeById(store.get().theme).map.labelHalo;
  const placed: { left: number; right: number; top: number; bottom: number }[] = [];
  for (const node of nodes) {
    if (!node.label) continue;
    const x = node.x + node.size + 3;
    const y = node.y + settings.labelSize / 3;
    const metrics = context.measureText(node.label);
    const pad = MAP_CONFIG.node.labelHaloWidth / 2 + 2;
    const left = x - metrics.actualBoundingBoxLeft - pad;
    const right = x + metrics.actualBoundingBoxRight + pad;
    const top = y - metrics.actualBoundingBoxAscent - pad;
    const bottom = y + metrics.actualBoundingBoxDescent + pad;
    if (placed.some(box => left < box.right && right > box.left && top < box.bottom && bottom > box.top)) continue;
    placed.push({ left, right, top, bottom });
    context.strokeText(node.label, x, y);
    context.fillStyle = node.color;
    context.fillText(node.label, x, y);
  }
}

const store = new Store<AppState>({
  screen: "menu",
  tab: 1,
  lang: "en",
  theme: "dark",
  selection: null,
  filters: {
    minCoauth: 1,
    minSharedAuthors: 1,
    yearMax: FILTER_CONFIG.year.max,
    showNoDeptAuthors: true,
    showNoDeptPubs: true,
    showExternalAuthors: false,
    showIsolatedAuthors: false,
    edgeZoomThreshold: FILTER_CONFIG.edgeZoom.default,
    labelZoomThreshold: FILTER_CONFIG.labelZoom.default,
    showRegions: { ...FILTER_CONFIG.showRegions },
    regionZoomThreshold: FILTER_CONFIG.regionZoom.default,
    regionMinNodes: FILTER_CONFIG.regionMinNodes.default,
  },
});

themeById(store.get().theme).apply(document.documentElement);
let appliedTheme = store.get().theme;
store.subscribe((state) => {
  if (state.theme === appliedTheme) return;
  appliedTheme = state.theme;
  themeById(state.theme).apply(document.documentElement);
});

const start = mountStart(store);
mountThemePicker(store);
start.setBootStage("loading");

loggedStep("graph-data.json", () => loadGraphData(DATA_CONFIG.graphDataUrl))
  .then((data) => {
    start.setBootStage("rendering");
    // Separate from the .catch() below, so a render failure is not reported
    // as a data loading failure.
    try {
      renderApp(data);
    } catch (error: unknown) {
      console.error("[rendering] failed after graph-data.json loaded:", error);
      start.setBootStage("error");
      start.hideBootOnError();
      showLoadError(t("start.errorRender", store.get().lang));
    }
  })
  .catch((error: unknown) => {
    console.error("[graph-data.json] failed to load:", error);
    start.setBootStage("error");
    start.hideBootOnError();
    showLoadError(
      `${t("start.errorFetch", store.get().lang)} (${DATA_CONFIG.graphDataUrl}). ${t("start.errorFetchHint", store.get().lang)}`,
    );
  });

function renderApp(data: GraphData): void {
  const pubDetailsByKey = new Map<string, PubDetail>();
  const authorDetailsByKey = new Map<string, AuthorDetail>();
  const repoDetailsByKey = new Map<string, RepoDetail>();
  const resources = {
    author: new DetailLoader(DATA_CONFIG.authorDetailsUrl, authorDetailsByKey, () =>
      store.notify(),
    ),
    pub: new DetailLoader(DATA_CONFIG.pubDetailsUrl, pubDetailsByKey, () => store.notify()),
    repo: new DetailLoader(DATA_CONFIG.repoDetailsUrl, repoDetailsByKey, () => store.notify()),
  };

  const container = requireElement("map");
  container.style.background = themeById(store.get().theme).map.background;

  const graph = new Graph();
  const initial = store.get();
  populateGraph(graph, data, initial.lang, initial.tab, initial.filters, pubDetailsByKey);

  const candidates: LabelNode[] = [];
  const priority: LabelNode[] = [];
  const renderer = new Sigma(graph, container, {
    stagePadding: MAP_CONFIG.fitPadding,
    enableEdgeEvents: true,
    enableCameraRotation: false,
    defaultDrawNodeHover: (_context, nodeData) => {
      const ratio = renderer.getCamera().getState().ratio;
      if (nodeData.highlighted || !isRegionMode(store.get(), ratio)) {
        priority.push(nodeData);
      }
    },
    defaultDrawNodeLabel: (_context, node) => candidates.push(node),
    // The camera threshold and collision check decide which candidate labels fit.
    labelRenderedSizeThreshold: 0,
    labelDensity: MAP_CONFIG.node.labelDensity,
    zoomingRatio: MAP_CONFIG.camera.zoomingRatio,
    maxCameraRatio: MAP_CONFIG.camera.maxRatio,
  });

  renderer.on("beforeRender", () => {
    candidates.length = 0;
    priority.length = 0;
  });
  renderer.on("afterRender", () => {
    const threshold = store.get().filters.labelZoomThreshold ?? FILTER_CONFIG.labelZoom.default;
    if (renderer.getCamera().getState().ratio > threshold) return;
    const context = renderer.getCanvases().labels?.getContext("2d");
    if (context) drawNodeLabels(context, priority.concat(candidates), renderer.getSettings());
  });

  // Before mountReactiveGraph, which drops a selection missing from the graph.
  mountUrlSync(store, data, { camera: renderer.getCamera(), button: requireElement("map-back") });
  mountHiddenAuthorReveal(store, data);
  const proximitySize = mountNodeProximity(renderer, store);
  mountReactiveGraph(renderer, store, data, pubDetailsByKey, proximitySize);
  let mapTheme = initial.theme;
  store.subscribe((state) => {
    if (state.theme === mapTheme) return;
    mapTheme = state.theme;
    container.style.background = themeById(state.theme).map.background;
    renderer.refresh();
  });

  const regions = mountRegions(renderer, store, data);
  mountSelection(renderer, store, regions.deptAtViewport);
  mountPanel(store, data, pubDetailsByKey, authorDetailsByKey, repoDetailsByKey, resources);
  mountDetailErrors(store, resources);
  mountMobilePanels(store);
  mountPanelToggles(store);
  new ResizeObserver(() => renderer.refresh()).observe(container);
  mountTabs(
    requireElement("tab-buttons"),
    requireElement("tab-content"),
    store,
    renderer,
    data,
    pubDetailsByKey,
    repoDetailsByKey,
  );
  mountFilters(store);
  mountZoomControls(renderer.getCamera(), store);
  mountGlobalSearch(store, data, pubDetailsByKey, repoDetailsByKey, authorDetailsByKey);

  console.info("Graph rendered (lists are ready, detail files load in the background):", {
    departments: data.departments.length,
    authors: data.authors.length,
    repos: data.repos.length,
    pubs: data.pubs.length,
  });

  for (const resource of Object.values(resources)) void resource.load();

  start.finishBoot();
}
