export const MAP_CONFIG = {
  fitPadding: 40,

  camera: {
    zoomingRatio: 1.45,
    // Camera ratio after flying to a selection. Sigma normalizes the layout,
    // so ratio 1 is always about the whole graph.
    focusRatio: 0.1,
    focusDuration: 600,
    maxRatio: 2,
  },

  node: {
    radius: 2,
    proximityDistance: 24,
    radiusNearCursor: 5,
    radiusSelected: 5,
    fallbackColor: "#9d9d9d",
    labelHaloWidth: 3,
    labelDensity: 1,
    labelMaxLength: 28,
  },

  edge: {
    width: 0.4,
    widthSelected: 1.4,
  },
} as const;

export const REGION_CONFIG = {
  // Kernel radius as a share of the mean node spacing, so dense and sparse
  // tabs get similar regions.
  kernelScale: 1,
  cellsPerSigma: 2.5,
  gridMaxSize: 400,
  densityThreshold: 0.35,
  smoothingRounds: 2,
  fillAlpha: 0.16,
  hoverFillAlpha: 0.32,
  strokeAlpha: 0.55,
  strokeWidth: 1.2,
  dimFactor: 0.3,
  labelSize: 13,
  labelWeight: "600",
  labelMaxWidth: 150,
  labelMaxLines: 3,
  labelLineHeight: 1.2,
  labelGap: 4,
} as const;

// Keep in sync with pauk/gui/graph_builder/config.py::NO_DEPT_COLOR.
export const NO_DEPT_COLOR = "#8a8f98";

export const FILTER_CONFIG = {
  coauth: { min: 1, max: 30 },
  sharedAuthors: { min: 1, max: 15 },
  year: { min: 2020, max: new Date().getFullYear() },
  // Regions appear above 0.25 or later. Edges and node labels end at 0.25 or earlier.
  edgeZoom: { min: 0.01, max: 0.25, step: 0.01, default: 0.25 },
  labelZoom: { min: 0.01, max: 0.25, step: 0.01, default: 0.25 },
  regionZoom: { min: 0.25, max: 1, step: 0.01, default: 0.25 },
  regionMinNodes: { min: 1, max: 50, default: 10 },
  showRegions: { 1: true, 2: true, 3: true },
  // Applying a filter rebuilds the whole graph, so sliders are debounced.
  debounceMs: 500,
} as const;

// Served from Vite publicDir. Missing files are handled by app/main.ts.
// BASE_URL is "/" on the lab server and "./" in the public build, which
// GitHub Pages serves from a subpath.
const BASE = import.meta.env.BASE_URL;
export const DATA_CONFIG = {
  graphDataUrl: `${BASE}graph-data.json`,
  authorDetailsUrl: `${BASE}authors-detail.json`,
  repoDetailsUrl: `${BASE}repos-detail.json`,
  pubDetailsUrl: `${BASE}pubs-detail.json`,
  reportsUrl: `${BASE}reports`,
  reportsIndexUrl: `${BASE}reports/index.json`,
  implementationRatesUrl: `${BASE}implementation-rates.json`,
} as const;

export const PANEL_CONFIG = {
  listLimit: 3,
  abstractWords: 50,
  chartBars: 8,
} as const;

export const TAB_LIST_CONFIG = { pageSize: 5 } as const;

export const SEARCH_CONFIG = {
  resultsLimit: 30,
} as const;
