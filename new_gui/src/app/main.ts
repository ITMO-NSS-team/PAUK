import "maplibre-gl/dist/maplibre-gl.css";
import { Map as MapLibreMap, NavigationControl } from "maplibre-gl";

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
