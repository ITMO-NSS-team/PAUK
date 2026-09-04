import "maplibre-gl/dist/maplibre-gl.css";
import { Map as MapLibreMap, NavigationControl, setWorkerUrl } from "maplibre-gl";
// The `?worker&url` query tells Vite to emit this as a self-contained
// worker chunk instead of pre-bundling it as a regular ESM dependency —
// without it Vite's dep optimizer looks for the worker file in the wrong
// place and the map fails at runtime with a "file does not exist" error.
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";

setWorkerUrl(workerUrl);

// Same placeholder style as the old GUI: this is a synthetic coordinate
// space for the graph layout, not a geographic basemap, so no tile
// provider or API key is needed.
const map = new MapLibreMap({
  container: "map",
  style: {
    version: 8,
    sources: {},
    layers: [{ id: "bg", type: "background", paint: { "background-color": "#ffffff" } }],
  },
  center: [0, 0],
  zoom: 5,
  minZoom: 4.3,
  maxZoom: 15,
  renderWorldCopies: false,
  attributionControl: false,
});

map.dragRotate.disable();
map.touchZoomRotate.disableRotation();
map.addControl(new NavigationControl({ showCompass: false }), "bottom-left");
