"use strict";

// ---------- state & URL routing ----------

var tab = 1;
var edgeMinCoauth = 1;
var edgeMinPub    = 1;
var edgeMinRepo   = 0;
var yearMax = 2026;
const YEAR_ABS_MAX = 2026;
const YEAR_ABS_MIN = 2020;

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

// ---------- preload / welcome / theme ----------

// Snaps the boot progress bar to 100% and reveals the map (deep link) or
// the welcome screen — also the only place to switch language for now.
function finishLoading() {
  if (typeof window._pauk_setProgress === "function") window._pauk_setProgress(1);
  const boot = document.getElementById("boot-screen");
  setTimeout(() => {
    if (boot) { boot.classList.add("hidden"); setTimeout(() => { boot.style.display = "none"; }, 350); }
    // Shows on every load, not just the first — it's the only language switch we have.
    if (!window._pauk_hasDeepLink) document.getElementById("welcome-modal").classList.remove("hidden");
  }, 200);
}

function hideWelcome() {
  document.getElementById("welcome-modal").classList.add("hidden");
}

function _applyMapTheme(dark) {
  if (!map || !map.getLayer) return;
  const cs = getComputedStyle(document.documentElement);
  const mapBg = cs.getPropertyValue("--map-bg").trim() || (dark ? "#191F1D" : "#ffffff");
  if (map.getLayer("bg"))        map.setPaintProperty("bg", "background-color", mapBg);
  if (map.getLayer("dept-line")) map.setPaintProperty("dept-line", "line-color", dark ? "#616161" : "#ffffff");
  if (map.getLayer("dept-edges"))
    map.setPaintProperty("dept-edges", "line-color", dark ? "#7F7F7F" : "#9D9D9D");
  if (map.getLayer("edges"))
    map.setPaintProperty("edges", "line-color", dark ? "#7F7F7F" : "#9D9D9D");
}

// Dark theme is paused (toggle hidden in style.css); this function still
// works, flip index.html's data-theme script back on to bring it back.
function _applyTheme(dark) {
  if (dark) document.documentElement.setAttribute("data-theme", "dark");
  else      document.documentElement.removeAttribute("data-theme");
  _applyMapTheme(dark);
  if (typeof refreshLabelColors === "function") { refreshLabelColors(); drawOverlay(); }
}

// ---------- filters ----------

function applyEdgeFilter() {
  if (tab === 1) {
    map.setFilter("edges", [">=", ["get", "w"], edgeMinCoauth]);
  } else if (tab === 3) {
    map.setFilter("edges", [">=", ["get", "w"], edgeMinPub]);
  } else if (tab === 2) {
    map.setFilter("edges", [">=", ["get", "w"], edgeMinRepo]);
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

function updateYearFilterUI(tabNum) {
  const card = document.getElementById("year-filter-card");
  if (tabNum === 3) {
    card.style.display = "";
    document.getElementById("year-from").value = yearMax;
    document.getElementById("year-from-val").textContent = yearMax;
  } else {
    card.style.display = "none";
    map.setFilter("pubs", null);
  }
}

function updateEdgeFilterUI(tabNum) {
  const card  = document.getElementById("edge-filter-card");
  const label = document.getElementById("edge-filter-label");
  const slider = document.getElementById("edge-threshold");
  const val    = document.getElementById("edge-threshold-val");
  if (tabNum === 1) {
    card.style.display = ""; label.textContent = t("filter.coauth");
    slider.min = 1; slider.step = 1; slider.max = 30;
    slider.value = edgeMinCoauth; val.textContent = edgeMinCoauth;
  } else if (tabNum === 3) {
    card.style.display = ""; label.textContent = t("filter.pubAuthors");
    slider.min = 1; slider.step = 1; slider.max = 15;
    slider.value = edgeMinPub; val.textContent = edgeMinPub;
  } else if (tabNum === 2) {
    // Repository edge weight is a sum of fractional signal shares, not a
    // count of anything, so this one steps in quarters from zero — the other
    // two tabs count whole publications and authors and start at one.
    card.style.display = ""; label.textContent = t("filter.repoStrength");
    slider.min = 0; slider.step = 0.25; slider.max = 4;
    slider.value = edgeMinRepo; val.textContent = edgeMinRepo;
  } else {
    card.style.display = "none";
  }
}

// ---------- map setup ----------

var map = new maplibregl.Map({
  container: "map",
  style: { version: 8, sources: {}, layers: [{ id: "bg", type: "background", paint: { "background-color": "#ffffff" } }] },
  center: [0, 0], zoom: 5, minZoom: 4.3, maxZoom: 15,
  renderWorldCopies: false, attributionControl: false, preserveDrawingBuffer: true,
});
map.dragRotate.disable();
map.touchZoomRotate.disableRotation();

map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "bottom-left");

map.on("load", () => {
  const hullData = buildHullFeatures();

  map.addSource("hulls",      { type: "geojson", data: hullData });
  map.addSource("dept-edges", { type: "geojson", data: buildBackboneFeatures() });
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
  map.addLayer({ id: "edges", type: "line", source: "edges",
    layout: { "line-cap": "round" },
    paint: {
      "line-color": "#9D9D9D",
      "line-width": ["interpolate", ["linear"], ["get", "w"], 1, 0.7, 5, 1.5, 20, 3],
      "line-opacity": EDGE_OPACITY_COAUTH,
    } });

  // Nodes render as raster "bubble" icons (circleImg/squareImg in
  // tab-authors.js/tab-pubs.js) — maplibre circle paint can't do an
  // inner gradient or a baked shadow, only a canvas can.
  map.addLayer({ id: "authors", type: "symbol", source: "nodes",
    filter: ["==", ["get", "kind"], "author"],
    layout: {
      "icon-image": ["get", "cid"],
      "icon-size": ["interpolate", ["linear"], ["zoom"],
        3, ["*", NODE_ICON_K.author.z3, ["get", "sz"]],
        9, ["*", NODE_ICON_K.author.z9, ["get", "sz"]]],
      "icon-allow-overlap": true, "icon-ignore-placement": true,
    },
    paint: { "icon-opacity": NODE_OPACITY } });
  map.addLayer({ id: "repos", type: "symbol", source: "nodes",
    filter: ["==", ["get", "kind"], "repo"],
    layout: {
      "icon-image": ["get", "cid"],
      "icon-size": ["interpolate", ["linear"], ["zoom"],
        3, ["*", NODE_ICON_K.repo.z3, ["get", "sz"]],
        9, ["*", NODE_ICON_K.repo.z9, ["get", "sz"]]],
      "icon-allow-overlap": true, "icon-ignore-placement": true,
    },
    paint: { "icon-opacity": NODE_OPACITY } });
  map.addLayer({ id: "pubs", type: "symbol", source: "nodes",
    filter: ["==", ["get", "kind"], "pub"],
    layout: {
      "icon-image": ["get", "sqid"],
      "icon-size": ["interpolate", ["linear"], ["zoom"],
        3, ["*", NODE_ICON_K.pub.z3, ["get", "sz"]],
        9, ["*", NODE_ICON_K.pub.z9, ["get", "sz"]]],
      "icon-allow-overlap": true, "icon-ignore-placement": true,
    },
    paint: {
      "icon-opacity": NODE_OPACITY,
      "icon-opacity-transition": { duration: 500, delay: 0 },
    } });

  // line-color is set per-selection in selectNode() (selection.js).
  map.addLayer({ id: "sel-edges", type: "line", source: "sel-edges",
    paint: { "line-color": "#181e1e", "line-width": 1, "line-opacity": 0.6 } });
  map.addLayer({ id: "sel-points", type: "symbol", source: "sel-points",
    layout: {
      "icon-image": ["get", "iid"],
      "icon-size": ["get", "isz"],
      "icon-allow-overlap": true, "icon-ignore-placement": true,
    } });

  map.on("click", e => {
    // Zoomed out on tabs 1/3: nodes are too tight to aim for, click resolves
    // to the department under the cursor. Departments only render there.
    if ((tab === 1 || tab === 3) && map.getZoom() < DEPT_CLICK_ZOOM) {
      const df = map.queryRenderedFeatures(e.point, { layers: ["dept-fill"] });
      if (df.length) { selectDept(df[0].properties.id); return; }
      clearAll();
      return;
    }
    const nf = map.queryRenderedFeatures(e.point, { layers: ["authors", "repos", "pubs"] });
    if (nf.length) { selectNode(nf[0].properties.key); return; }
    const ef = map.queryRenderedFeatures(e.point, { layers: ["edges"] });
    if (ef.length) { selectEdge(ef[0].properties); return; }
    // Repositories stay visible at every zoom, so their tab never falls into
    // the department-click branch above — the territory is still worth a click.
    const df2 = map.queryRenderedFeatures(e.point, { layers: ["dept-fill"] });
    if (df2.length) { selectDept(df2[0].properties.id); return; }
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
    if (tab === 2) edgeMinRepo   = v;
    applyEdgeFilter();
  });

  const yearSlider = document.getElementById("year-from");
  yearSlider.min = YEAR_ABS_MIN; yearSlider.max = YEAR_ABS_MAX; yearSlider.value = YEAR_ABS_MAX;

  document.getElementById("year-from").addEventListener("input", function () {
    yearMax = +this.value;
    document.getElementById("year-from-val").textContent = yearMax;
    applyYearFilter();
  });

  renderOverview();
  const _initCam = map.cameraForBounds(FIT, { padding: 30 });
  map.easeTo({ center: _initCam ? _initCam.center : [0, 0], zoom: 5, duration: 0 });
  finishLoading();
  sizeOverlay();
  map.on("render", drawOverlay);
  drawOverlay();

  _applyMapTheme(document.documentElement.getAttribute("data-theme") === "dark");

  document.getElementById("theme-toggle").addEventListener("click", function () {
    _applyTheme(document.documentElement.getAttribute("data-theme") !== "dark");
  });

  document.getElementById("welcome-close").addEventListener("click", hideWelcome);

  // Push the landing page into history first for profile URLs, so "back" leads there.
  const _initParams = Object.fromEntries(new URLSearchParams(location.search));
  let _initTab = +(_initParams.tab || 1);
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

// ---------- tab switching ----------

function setTab(t) {
  if (t === tab) return;
  tab = t;

  const isSearch = t === 4;
  const isStats  = t === 5;
  const isPage   = isSearch || isStats;   // full-screen tabs that hide the map chrome
  document.getElementById("search-page").classList.toggle("visible", isSearch);
  document.getElementById("stats-page").classList.toggle("visible", isStats);
  document.getElementById("left-panel").style.display  = isPage ? "none" : "";
  document.getElementById("right-panel").style.display = isPage ? "none" : "";

  if (!isPage) {
    // Territories are drawn on every map tab; the department backbone is not —
    // it is built from co-authorship between departments, which says nothing
    // about how two repositories relate.
    const showDept = t === 1 || t === 2 || t === 3;
    for (const l of ["dept-fill", "dept-line"])
      map.setLayoutProperty(l, "visibility", showDept ? "visible" : "none");
    map.setLayoutProperty("dept-edges", "visibility", t === 2 ? "none" : "visible");
    map.setPaintProperty("repos", "icon-opacity", t === 2 ? 1 : NODE_OPACITY);
    map.setPaintProperty("edges", "line-opacity", t === 2 ? 1 : t === 3 ? EDGE_OPACITY_PUBS : EDGE_OPACITY_COAUTH);
    clearAll();
    map.getSource("hulls").setData(buildHullFeatures());
    map.getSource("dept-edges").setData(buildBackboneFeatures());
    map.getSource("edges").setData(buildEdgeFeatures());
    map.getSource("nodes").setData(buildNodeFeatures());
    const _cam = map.cameraForBounds(FIT, { padding: 30 });
    map.easeTo({ center: _cam ? _cam.center : [0, 0], zoom: 5, duration: 400 });
    updateEdgeFilterUI(t);
    updateYearFilterUI(t);
    applyEdgeFilter();
    if (t === 3) applyYearFilter();
    renderOverview();
  } else if (isSearch) {
    spShowLanding();
  } else {
    renderStats();
  }

  document.querySelectorAll("#tab-toggle button").forEach(b => {
    b.classList.toggle("active", +b.dataset.tab === t);
  });
  document.title = "PAUK";
  _replaceUrl({ tab: t });
}

console.log(`PAUK: авторов ${DATA.authors.length}, репозиториев ${DATA.repos.length}, публикаций ${DATA.pubs.length}`);