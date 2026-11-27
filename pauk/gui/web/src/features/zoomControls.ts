import type Sigma from "sigma";
import { MAP_CONFIG } from "../core/config";
import { requireElement } from "../core/dom";
import { t } from "../core/i18n";
import type { AppState, Store } from "../core/state";

export function mountZoomControls(camera: ReturnType<Sigma["getCamera"]>, store: Store<AppState>): void {
  const out = requireElement("zoom-out");
  const reset = requireElement("zoom-reset");
  const zoomIn = requireElement("zoom-in");

  out.onclick = () => void camera.animatedUnzoom(MAP_CONFIG.camera.zoomingRatio);
  reset.onclick = () => void camera.animatedReset();
  zoomIn.onclick = () => void camera.animatedZoom(MAP_CONFIG.camera.zoomingRatio);

  function renderRatio(): void {
    const ratio = camera.getState().ratio.toFixed(3);
    if (reset.textContent !== ratio) reset.textContent = ratio;
  }
  camera.on("updated", renderRatio);
  renderRatio();

  function renderLabels({ lang }: AppState): void {
    const labels = [
      [out, t("map.zoomOut", lang)],
      [reset, t("map.zoomReset", lang)],
      [zoomIn, t("map.zoomIn", lang)],
    ] as const;
    for (const [button, label] of labels) {
      button.title = label;
      button.setAttribute("aria-label", label);
    }
  }
  renderLabels(store.get());
  store.subscribe(renderLabels);
}
