import Graph from "graphology";
import Sigma from "sigma";
import type { Settings } from "sigma/settings";
import type { NodeDisplayData, PartialButFor } from "sigma/types";
import type { AuthorDetail, GraphData, PubDetail, RepoDetail } from "../contracts/graph";
import { DATA_CONFIG, FILTER_CONFIG, MAP_CONFIG } from "../core/config";
import { loadDetails, loadGraphData, mergeDetailsInto } from "../core/data";
import { requireElement, showLoadError } from "../core/dom";
import { t } from "../core/i18n";
import { loggedStep } from "../core/log";
import { isRegionMode, Store, type AppState } from "../core/state";
import { themeById } from "../core/themes";
import { mountFilters, mountHiddenAuthorReveal } from "../features/filters";
import { mountGlobalSearch } from "../features/globalSearch";
import { mountPanel } from "../features/panels";
import { mountSelection } from "../features/selection";
import { mountStart } from "../features/start";
import { mountTabs } from "../features/tabs";
import { mountThemePicker } from "../features/themePicker";
import { mountUrlSync } from "../features/urlSync";
import { mountReactiveGraph, mountZoomDebug, populateGraph } from "../map/build";
import { mountRegions } from "../map/regions";

// Node label in its department color with a halo. Also used for hover and
// selection, with no extra glow. Lives here, not in map/build.ts: runtime
// imports from "sigma" touch WebGL, which the jsdom tests lack.
function drawHaloedNodeLabel(
  context: CanvasRenderingContext2D,
  data: PartialButFor<NodeDisplayData, "x" | "y" | "size" | "label" | "color">,
  settings: Settings,
): void {
  if (!data.label) return;

  context.font = `${settings.labelWeight} ${settings.labelSize}px ${settings.labelFont}`;
  const x = data.x + data.size + 3;
  const y = data.y + settings.labelSize / 3;

  context.lineJoin = "round";
  context.lineWidth = MAP_CONFIG.node.labelHaloWidth;
  context.strokeStyle = themeById(store.get().theme).map.labelHalo;
  context.strokeText(data.label, x, y);

  context.fillStyle = data.color;
  context.fillText(data.label, x, y);
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
    showRegions: { ...FILTER_CONFIG.showRegions },
    regionZoomThreshold: FILTER_CONFIG.regionZoom.default,
    regionMinNodes: FILTER_CONFIG.regionMinNodes.default,
  },
});

// Before the data loads, so the menu and boot screen already use the theme.
themeById(store.get().theme).apply(document.documentElement);
let appliedTheme = store.get().theme;
store.subscribe((state) => {
  if (state.theme === appliedTheme) return;
  appliedTheme = state.theme;
  themeById(state.theme).apply(document.documentElement);
});

function loadDetailsInto<T extends { key: string }>(
  name: string,
  url: string,
  target: Map<string, T>,
): void {
  loggedStep(name, () => loadDetails<T>(url))
    .then((details) => {
      mergeDetailsInto(target, details);
      store.notify();
    })
    .catch(() => {
      // Already logged by loggedStep. No banner: a missing detail file (e.g.
      // no authors-detail.json in public/) leaves those fields loading.
    });
}

// Mounted before the first fetch, so the boot screen shows from the first frame.
const start = mountStart(store);
mountThemePicker(store);
start.setBootStage("loading");

// graph-data.json first; detail files load in the background after the first render.
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
  // Empty maps are shared with all features and filled in place when the
  // detail files arrive, followed by store.notify().
  const pubDetailsByKey = new Map<string, PubDetail>();
  const authorDetailsByKey = new Map<string, AuthorDetail>();
  const repoDetailsByKey = new Map<string, RepoDetail>();

  // A page load always opens the menu and ignores the URL; parseUrlState
  // is used only for back/forward in urlSync.

  const container = requireElement("map");
  container.style.background = themeById(store.get().theme).map.background;

  const graph = new Graph();
  const initial = store.get();
  populateGraph(graph, data, initial.lang, initial.tab, initial.filters, pubDetailsByKey);

  const renderer = new Sigma(graph, container, {
    stagePadding: MAP_CONFIG.fitPadding,
    enableEdgeEvents: true,
    enableCameraRotation: false,
    defaultDrawNodeHover: (context, nodeData, settings) => {
      // In region mode the hovered node is not labelled; the selected one is.
      const ratio = renderer.getCamera().getState().ratio;
      if (nodeData.highlighted || !isRegionMode(store.get(), ratio)) {
        drawHaloedNodeLabel(context, nodeData, settings);
      }
    },
    defaultDrawNodeLabel: drawHaloedNodeLabel,
    labelRenderedSizeThreshold: MAP_CONFIG.node.labelVisibleAtSize,
    labelDensity: MAP_CONFIG.node.labelDensity,
    zoomingRatio: MAP_CONFIG.camera.zoomingRatio,
    maxCameraRatio: MAP_CONFIG.camera.maxRatio,
  });

  // Before mountReactiveGraph, which drops a selection missing from the graph.
  mountHiddenAuthorReveal(store, data);
  mountReactiveGraph(renderer, store, data, pubDetailsByKey);
  // Map colors are read from the theme on every render, so a theme change
  // needs only the container background and a refresh.
  let mapTheme = initial.theme;
  store.subscribe((state) => {
    if (state.theme === mapTheme) return;
    mapTheme = state.theme;
    container.style.background = themeById(state.theme).map.background;
    renderer.refresh();
  });
  // TODO: remove once zoom thresholds are tuned.
  mountZoomDebug(renderer);

  // Features talk only through the Store and live for the whole page.
  const regions = mountRegions(renderer, store, data);
  mountSelection(renderer, store, regions.deptAtViewport);
  mountPanel(store, data, pubDetailsByKey, authorDetailsByKey, repoDetailsByKey);
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
  mountGlobalSearch(store, data, pubDetailsByKey, repoDetailsByKey, authorDetailsByKey);
  mountUrlSync(store, data);

  console.info("Graph rendered (lists are ready, detail files load in the background):", {
    departments: data.departments.length,
    authors: data.authors.length,
    repos: data.repos.length,
    pubs: data.pubs.length,
  });

  // Each detail file merges as soon as it arrives. The panel shows a loading
  // indicator until then.
  loadDetailsInto<PubDetail>("pubs-detail.json", DATA_CONFIG.pubDetailsUrl, pubDetailsByKey);
  loadDetailsInto<AuthorDetail>(
    "authors-detail.json",
    DATA_CONFIG.authorDetailsUrl,
    authorDetailsByKey,
  );
  loadDetailsInto<RepoDetail>("repos-detail.json", DATA_CONFIG.repoDetailsUrl, repoDetailsByKey);

  start.finishBoot();
}
