"use strict";

var tab = 1;
var edgeMinCoauth = 1;
var edgeMinPub    = 1;
var yearMax = 2026;
const YEAR_ABS_MAX = 2026;
const YEAR_ABS_MIN = 2020;
const PLAY_STEP_MS = 1500;

var _playTimer = null;
var _routingFromPop = false;

function _pushUrl(params) {
  if (_routingFromPop) return;
  history.pushState(params, "", "?" + new URLSearchParams(params));
}

function _replaceUrl(params) {
  if (_routingFromPop) return;
  history.replaceState(params, "", "?" + new URLSearchParams(params));
}

function _applyUrlState(state) {
  const t = +(state.tab || 1);
  if (t !== tab) setTab(t);
  if (t === 4) {
    if      (state.kind === "author" && state.key)     spShowAuthorProfile(state.key);
    else if (state.kind === "pub"    && state.key)     spShowPubProfile(state.key);
    else if (state.kind === "repo"   && state.key)     spShowRepoProfile(state.key);
    else if (state.kind === "dept"   && state.id != null) spShowDeptProfile(state.id);
    else spShowLanding();
  }
}

window.addEventListener("popstate", e => {
  _routingFromPop = true;
  _applyUrlState(e.state || { tab: 1 });
  _routingFromPop = false;
});

// Opacity expression: current year = full brightness, past years = dimmed
// (zoom thresholds are kept in sync with NODE_OPACITY)
function _yearOpacityExpr(year) {
  return ["case",
    ["==", ["get", "year"], year],
    ["interpolate", ["linear"], ["zoom"], 4.5, 0, 6.0, 1.0],
    ["interpolate", ["linear"], ["zoom"], 4.5, 0, 6.0, 0.5]
  ];
}

function _applyYearStep() {
  document.getElementById("year-from").value = yearMax;
  document.getElementById("year-from-val").textContent = yearMax;
  applyYearFilter();
}

function stopPlay() {
  if (_playTimer) { clearInterval(_playTimer); _playTimer = null; }
  const btn = document.getElementById("year-play-btn");
  if (btn) btn.textContent = "▶";
  // Restore uniform opacity
  map.setPaintProperty("pubs", "icon-opacity", NODE_OPACITY);
}

function startPlay() {
  yearMax = YEAR_ABS_MIN;
  map.setPaintProperty("pubs", "icon-opacity", _yearOpacityExpr(yearMax));
  _applyYearStep();
  document.getElementById("year-play-btn").textContent = "⏸";

  _playTimer = setInterval(() => {
    if (yearMax >= YEAR_ABS_MAX) { stopPlay(); return; }
    yearMax++;
    map.setPaintProperty("pubs", "icon-opacity", _yearOpacityExpr(yearMax));
    _applyYearStep();
  }, PLAY_STEP_MS);
}

function togglePlay() {
  _playTimer ? stopPlay() : startPlay();
}

// index.html's inline script already shows the hero/boot screen and starts
// the rotating status text (graph-data.js blocks this file from running any
// sooner) — this just stops the rotation and reveals whatever's ready.
function finishLoading() {
  clearInterval(window._pauk_rotateTimer);
  if (window._pauk_hasDeepLink) {
    const el = document.getElementById("boot-screen");
    if (!el) return;
    el.classList.add("hidden");
    setTimeout(() => { el.style.display = "none"; }, 350);
  } else {
    const cta = document.getElementById("welcome-close");
    cta.disabled = false;
    cta.classList.remove("loading");
    document.getElementById("welcome-cta-text").textContent = "Смотреть карту";
  }
}

function hideWelcome() {
  document.getElementById("welcome-modal").classList.add("hidden");
}

function exportPng() {
  const mapCanvas = map.getCanvas();
  const overlay   = document.getElementById("overlay");
  const dpr = window.devicePixelRatio || 1;
  const w = Math.round(window.innerWidth  * dpr);
  const h = Math.round(window.innerHeight * dpr);

  const out = document.createElement("canvas");
  out.width = w; out.height = h;
  const ctx = out.getContext("2d");

  ctx.drawImage(mapCanvas, 0, 0, w, h);
  ctx.drawImage(overlay,   0, 0, w, h);

  // branding — pill in the bottom-right corner
  const label = "PAUK · Карта соавторства ИТМО";
  const fs = Math.round(11 * dpr);
  ctx.font = `600 ${fs}px -apple-system, BlinkMacSystemFont, sans-serif`;
  const tw = ctx.measureText(label).width;
  const px = 10 * dpr, py = 6 * dpr, r = 6 * dpr;
  const bw = tw + px * 2, bh = (20 * dpr);
  const bx = w - bw - 12 * dpr, by = h - bh - 10 * dpr;
  ctx.fillStyle = "rgba(0,0,0,0.48)";
  if (ctx.roundRect) {
    ctx.beginPath();
    ctx.roundRect(bx, by, bw, bh, r);
    ctx.fill();
  } else {
    ctx.fillRect(bx, by, bw, bh);
  }
  ctx.fillStyle = "rgba(255,255,255,0.92)";
  ctx.fillText(label, bx + px, by + bh - py - 1);

  out.toBlob(blob => {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "pauk-map.png";
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  });
}

// Theme-dependent map layer colors; background comes from the --map-bg CSS variable.
function _applyMapTheme(dark) {
  if (!map || !map.getLayer) return;
  const mapBg = getComputedStyle(document.documentElement)
    .getPropertyValue("--map-bg").trim() || (dark ? "#191F1D" : "#ffffff");
  if (map.getLayer("bg"))        map.setPaintProperty("bg", "background-color", mapBg);
  if (map.getLayer("dept-line")) map.setPaintProperty("dept-line", "line-color", dark ? "#616161" : "#ffffff");
  if (map.getLayer("authors"))   map.setPaintProperty("authors", "circle-stroke-color",
    dark ? "rgba(242,242,242,0.25)" : "rgba(25,31,29,0.28)");
  if (map.getLayer("dept-edges"))
    map.setPaintProperty("dept-edges", "line-color", dark ? "#7F7F7F" : "#9D9D9D");
  if (map.getLayer("dept-focus-edges"))
    map.setPaintProperty("dept-focus-edges", "line-color", dark ? "#F2F2F2" : "#191F1D");
  if (map.getLayer("sel-edges"))
    map.setPaintProperty("sel-edges", "line-color", dark ? "#F2F2F2" : "#191F1D");
}

function _applyTheme(dark) {
  // no localStorage: every fresh load starts dark by design
  if (dark) document.documentElement.setAttribute("data-theme", "dark");
  else      document.documentElement.removeAttribute("data-theme");
  _applyMapTheme(dark);
  if (typeof refreshLabelColors === "function") { refreshLabelColors(); drawOverlay(); }
}

function applyEdgeFilter() {
  if (tab === 1) {
    map.setFilter("edges", [">=", ["get", "w"], edgeMinCoauth]);
  } else if (tab === 3) {
    map.setFilter("edges", [">=", ["get", "w"], edgeMinPub]);
  } else {
    map.setFilter("edges", null);
  }
}

function applyYearFilter() {
  if (tab !== 3) return;
  const f = yearMax === YEAR_ABS_MAX ? null : ["<=", ["get", "year"], yearMax];
  map.setFilter("pubs", f);
  map.getSource("edges").setData(buildEdgeFeatures());
}

function updateYearFilterUI(t) {
  const card = document.getElementById("year-filter-card");
  if (t === 3) {
    card.style.display = "";
    document.getElementById("year-from").value = yearMax;
    document.getElementById("year-from-val").textContent = yearMax;
  } else {
    card.style.display = "none";
    map.setFilter("pubs", null);
  }
}

function updateEdgeFilterUI(t) {
  const card  = document.getElementById("edge-filter-card");
  const label = document.getElementById("edge-filter-label");
  const slider = document.getElementById("edge-threshold");
  const val    = document.getElementById("edge-threshold-val");
  if (t === 1) {
    card.style.display = ""; label.textContent = "мин. совм. публикаций";
    slider.max = 30; slider.value = edgeMinCoauth; val.textContent = edgeMinCoauth;
  } else if (t === 3) {
    card.style.display = ""; label.textContent = "мин. общих авторов ИТМО";
    slider.max = 15; slider.value = edgeMinPub; val.textContent = edgeMinPub;
  } else {
    card.style.display = "none";
  }
}

var map = new maplibregl.Map({
  container: "map",
  style: { version: 8, sources: {}, layers: [{ id: "bg", type: "background", paint: { "background-color": "#ffffff" } }] },
  center: [0, 0], zoom: 5.3, minZoom: 4.3, maxZoom: 15,
  renderWorldCopies: false, attributionControl: false, preserveDrawingBuffer: true,
});
map.dragRotate.disable();
map.touchZoomRotate.disableRotation();

map.on("load", () => {
  const hullData = buildHullFeatures();

  map.addSource("hulls",      { type: "geojson", data: hullData });
  map.addSource("dept-edges", { type: "geojson", data: buildBackboneFeatures() });
  map.addSource("dept-focus", { type: "geojson", data: empty() });
  map.addSource("edges",      { type: "geojson", data: buildEdgeFeatures() });
  map.addSource("nodes",      { type: "geojson", data: buildNodeFeatures() });
  map.addSource("sel-edges",  { type: "geojson", data: empty() });
  map.addSource("sel-points", { type: "geojson", data: empty() });

  map.addLayer({ id: "dept-fill", type: "fill", source: "hulls",
    paint: { "fill-color": ["get", "color"], "fill-opacity": FILL_OPACITY } });
  map.addLayer({ id: "dept-line", type: "line", source: "hulls",
    paint: { "line-color": "#ffffff", "line-opacity": 0.8, "line-width": 1.4 } });

  map.addLayer({ id: "dept-edges", type: "line", source: "dept-edges",
    layout: { "line-cap": "round" },
    paint: {
      "line-color": "#9D9D9D",
      "line-width": ["interpolate", ["linear"], ["get", "w"], 2, 0.6, 50, 4, 385, 8],
      "line-opacity": DEPT_EDGE_OPACITY,
    } });
  map.addLayer({ id: "dept-focus-edges", type: "line", source: "dept-focus",
    layout: { "line-cap": "round" },
    paint: {
      "line-color": "#191F1D",
      "line-width": ["interpolate", ["linear"], ["get", "w"], 1, 1, 50, 5, 385, 9],
      "line-opacity": FOCUS_EDGE_OPACITY,
    } });

  map.addLayer({ id: "edges", type: "line", source: "edges",
    layout: { "line-cap": "round" },
    paint: {
      "line-color": ["match", ["get", "kind"],
        "coauth",    "#4e7bbf",
        "repo_auth", "#e67e22",
        "repo_pub",  "#9b59b6",
        "#4e7bbf",
      ],
      "line-width": ["interpolate", ["linear"], ["get", "w"], 1, 0.7, 5, 1.5, 20, 3],
      "line-opacity": EDGE_OPACITY_COAUTH,
    } });

  map.addLayer({ id: "authors", type: "circle", source: "nodes",
    filter: ["==", ["get", "kind"], "author"],
    paint: {
      "circle-color": ["get", "color"],
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 3, ["*", 0.8, ["get", "sz"]], 9, ["*", 5, ["get", "sz"]]],
      "circle-stroke-color": "rgba(0,0,0,0.28)", "circle-stroke-width": 0.6,
      "circle-opacity": NODE_OPACITY,
    } });
  map.addLayer({ id: "repos", type: "circle", source: "nodes",
    filter: ["==", ["get", "kind"], "repo"],
    paint: {
      "circle-color": ["get", "color"],
      "circle-radius": ["interpolate", ["linear"], ["zoom"],
        3, ["*", 1.5, ["get", "sz"]],
        9, ["*", 7,   ["get", "sz"]]],
      "circle-stroke-color": "#ffffff", "circle-stroke-width": 1.5,
      "circle-opacity": NODE_OPACITY,
    } });
  map.addLayer({ id: "pubs", type: "symbol", source: "nodes",
    filter: ["==", ["get", "kind"], "pub"],
    layout: {
      "icon-image": ["get", "sqid"],
      "icon-size": ["interpolate", ["linear"], ["zoom"], 3, ["*", 0.18, ["get", "sz"]], 9, ["*", 1.1, ["get", "sz"]]],
      "icon-allow-overlap": true, "icon-ignore-placement": true,
    },
    paint: {
      "icon-opacity": NODE_OPACITY,
      "icon-opacity-transition": { duration: 500, delay: 0 },
    } });

  map.addLayer({ id: "sel-edges", type: "line", source: "sel-edges",
    paint: { "line-color": "#191F1D", "line-width": 1.8, "line-opacity": 0.9 } });
  map.addLayer({ id: "sel-points", type: "circle", source: "sel-points",
    paint: {
      "circle-color": ["get", "color"],
      "circle-radius": ["case", ["==", ["get", "role"], "focus"], 8, 5],
      "circle-stroke-color": "#ffffff", "circle-stroke-width": 2,
    } });

  map.on("click", e => {
    const nf = map.queryRenderedFeatures(e.point, { layers: ["authors", "repos", "pubs"] });
    if (nf.length) { selectNode(nf[0].properties.key); return; }
    const ef = map.queryRenderedFeatures(e.point, { layers: ["edges"] });
    if (ef.length) { selectEdge(ef[0].properties); return; }
    const df = map.queryRenderedFeatures(e.point, { layers: ["dept-fill"] });
    if (df.length) { selectDept(df[0].properties.id); return; }
    clearAll();
  });

  for (const l of ["authors", "repos", "pubs", "dept-fill", "edges"]) {
    map.on("mouseenter", l, () => (map.getCanvas().style.cursor = "pointer"));
    map.on("mouseleave", l, () => (map.getCanvas().style.cursor = ""));
  }

  document.querySelectorAll("#tab-toggle button").forEach(b => {
    b.onclick = () => setTab(+b.dataset.tab);
  });

  applyEdgeFilter();
  updateEdgeFilterUI(1);
  updateYearFilterUI(1);

  document.getElementById("edge-threshold").addEventListener("input", function () {
    const v = +this.value;
    document.getElementById("edge-threshold-val").textContent = v;
    if (tab === 1) edgeMinCoauth = v;
    if (tab === 3) edgeMinPub    = v;
    applyEdgeFilter();
  });

  document.getElementById("year-play-btn").addEventListener("click", togglePlay);

  const yearSlider = document.getElementById("year-from");
  yearSlider.min = YEAR_ABS_MIN; yearSlider.max = YEAR_ABS_MAX; yearSlider.value = YEAR_ABS_MAX;

  document.getElementById("year-from").addEventListener("input", function () {
    if (_playTimer) stopPlay();
    yearMax = +this.value;
    document.getElementById("year-from-val").textContent = yearMax;
    applyYearFilter();
  });

  renderOverview();
  const _initCam = map.cameraForBounds(FIT, { padding: 30 });
  map.easeTo({ center: _initCam ? _initCam.center : [0, 0], zoom: 5.6, duration: 0 });
  finishLoading();
  sizeOverlay();
  map.on("render", drawOverlay);
  drawOverlay();

  _applyMapTheme(document.documentElement.getAttribute("data-theme") === "dark");

  document.getElementById("theme-toggle").addEventListener("click", function () {
    _applyTheme(document.documentElement.getAttribute("data-theme") !== "dark");
  });

  document.getElementById("export-btn").addEventListener("click", exportPng);
  document.getElementById("welcome-close").addEventListener("click", hideWelcome);

  // Initial routing. For profile URLs, push the landing page into history first,
  // so the browser "back" button leads there instead of leaving the site.
  const _initParams = Object.fromEntries(new URLSearchParams(location.search));
  const _initTab = +(_initParams.tab || 1);
  if (_initTab === 4 && window._pauk_hasDeepLink) {
    history.replaceState({ tab: 4 }, "", "?tab=4");
    _applyUrlState(_initParams);
  } else {
    _routingFromPop = true;
    _applyUrlState({ tab: _initTab });
    _routingFromPop = false;
    history.replaceState({ tab: _initTab }, "", "?tab=" + _initTab);
  }
});

function setTab(t) {
  if (t === tab) return;
  if (_playTimer) stopPlay();
  tab = t;

  const isSearch = t === 4;
  document.getElementById("search-page").classList.toggle("visible", isSearch);
  document.getElementById("left-panel").style.display  = isSearch ? "none" : "";
  document.getElementById("right-panel").style.display = isSearch ? "none" : "";

  if (!isSearch) {
    const showDept = t === 1;
    for (const l of ["dept-fill", "dept-line", "dept-edges", "dept-focus-edges"])
      map.setLayoutProperty(l, "visibility", showDept ? "visible" : "none");
    map.setPaintProperty("repos", "circle-opacity", t === 2 ? 1 : NODE_OPACITY);
    map.setPaintProperty("edges", "line-opacity", t === 2 ? 1 : t === 3 ? EDGE_OPACITY_PUBS : EDGE_OPACITY_COAUTH);
    clearAll();
    map.getSource("hulls").setData(buildHullFeatures());
    map.getSource("dept-edges").setData(buildBackboneFeatures());
    map.getSource("edges").setData(buildEdgeFeatures());
    map.getSource("nodes").setData(buildNodeFeatures());
    const _cam = map.cameraForBounds(FIT, { padding: 30 });
    map.easeTo({ center: _cam ? _cam.center : [0, 0], zoom: t === 2 ? 5.2 : 5.6, duration: 400 });
    updateEdgeFilterUI(t);
    updateYearFilterUI(t);
    applyEdgeFilter();
    if (t === 3) applyYearFilter();
    renderOverview();
  } else {
    spShowLanding();
  }

  document.querySelectorAll("#tab-toggle button").forEach(b => {
    b.classList.toggle("active", +b.dataset.tab === t);
  });
  document.title = "PAUK";
  _replaceUrl({ tab: t });
}

console.log(`PAUK: авторов ${DATA.authors.length}, репозиториев ${DATA.repos.length}, публикаций ${DATA.pubs.length}`);
