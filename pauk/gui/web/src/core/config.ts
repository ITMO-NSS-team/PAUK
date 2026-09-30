export const MAP_CONFIG = {
  // Sigma stagePadding.
  fitPadding: 40,

  camera: {
    // Sigma zoomingRatio: ratio change per wheel tick (Sigma default 1.7).
    zoomingRatio: 1.45,
    // Camera ratio after flying to a selection. Sigma normalizes the layout,
    // so ratio 1 is always about the whole graph.
    focusRatio: 0.1,
    // ms
    focusDuration: 600,
    // Sigma maxCameraRatio.
    maxRatio: 2,
  },

  node: {
    radius: 3,
    radiusSelected: 5,
    // Department without a color; should not happen on real data.
    fallbackColor: "#9d9d9d",
    // Stroke around the department-colored label text.
    labelHaloWidth: 3,
    // Sigma labelRenderedSizeThreshold: labels show only on nodes at least
    // this many pixels wide. Lower means more labels.
    labelVisibleAtSize: 20,
    // Sigma labelDensity.
    labelDensity: 1,
    // Map labels only; lists and search show the full label.
    labelMaxLength: 28,
  },

  edge: {
    width: 0.4,
    widthSelected: 1.4,
  },
} as const;

// Department regions (map/regions.ts).
export const REGION_CONFIG = {
  // Kernel radius as a share of the mean node spacing, so dense and sparse
  // tabs get similar regions.
  kernelScale: 1,
  // Grid cells per kernel sigma (contour precision).
  cellsPerSigma: 2.5,
  // Grid size cap on the long side (rebuild time).
  gridMaxSize: 400,
  // Minimum density for a cell to belong to a department; one node peaks at 1.
  densityThreshold: 0.35,
  // Chaikin smoothing rounds.
  smoothingRounds: 2,
  fillAlpha: 0.16,
  // Region under the cursor in region mode.
  hoverFillAlpha: 0.32,
  strokeAlpha: 0.55,
  strokeWidth: 1.2,
  // Dimming of regions outside the selection.
  dimFactor: 0.3,
  labelSize: 13,
  labelWeight: "600",
  // px; longer names wrap.
  labelMaxWidth: 150,
  // Last line ends with an ellipsis.
  labelMaxLines: 3,
  labelLineHeight: 1.2,
  // Overlapping labels of smaller regions are skipped.
  labelGap: 4,
} as const;

// Keep in sync with pauk/gui/graph_builder/config.py::NO_DEPT_COLOR.
export const NO_DEPT_COLOR = "#8a8f98";

// Slider ranges; current values live in AppState.filters.
export const FILTER_CONFIG = {
  coauth: { min: 1, max: 30 },
  sharedAuthors: { min: 1, max: 15 },
  year: { min: 2020, max: new Date().getFullYear() },
  // Camera ratio thresholds. edgeZoom.max equals regionZoom.min, so edges and
  // regions are never drawn at the same time.
  edgeZoom: { min: 0.05, max: 0.25, step: 0.01, default: 0.25 },
  regionZoom: { min: 0.25, max: 1, step: 0.01, default: 0.4 },
  regionMinNodes: { min: 1, max: 50, default: 10 },
  // Tabs with regions on at start: authors and pubs.
  showRegions: { 1: true, 2: false, 3: true },
  // Applying a filter rebuilds the whole graph, so sliders are debounced.
  debounceMs: 500,
} as const;

// Served from Vite publicDir. Missing files are handled by app/main.ts.
export const DATA_CONFIG = {
  graphDataUrl: "/graph-data.json",
  authorDetailsUrl: "/authors-detail.json",
  repoDetailsUrl: "/repos-detail.json",
  pubDetailsUrl: "/pubs-detail.json",
  reportsIndexUrl: "/reports/index.json",
  implementationRatesUrl: "/implementation-rates.json",
} as const;

// Top-N lists in the info card.
export const PANEL_CONFIG = {
  listLimit: 3,
  // Words shown before "Read more".
  abstractWords: 50,
  // Bars in overview charts.
  chartBars: 8,
} as const;

export const TAB_LIST_CONFIG = { pageSize: 10 } as const;

// Larger than listLimit: search filters by substring and does not rank.
export const SEARCH_CONFIG = {
  resultsLimit: 30,
} as const;
