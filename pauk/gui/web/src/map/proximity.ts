import type Sigma from "sigma";
import { MAP_CONFIG } from "../core/config";
import { isRegionMode, type AppState, type Store } from "../core/state";

export function mountNodeProximity(renderer: Sigma, store: Store<AppState>): (key: string) => number | undefined {
  const graph = renderer.getGraph();
  const container = renderer.getContainer();
  const { radius, radiusNearCursor, proximityDistance } = MAP_CONFIG.node;
  let sorted: { key: string; x: number; y: number }[] | null = null;
  let active: string | null = null;
  let activeSize: number = radius;
  let frame = 0;
  let pointer = { x: 0, y: 0 };

  function resize(key: string | null, size: number = radius): void {
    if (active === key && activeSize === size) return;
    const previous = active;
    active = key;
    activeSize = size;
    const nodes = [];
    if (previous && previous !== key && graph.hasNode(previous)) nodes.push(previous);
    if (key) nodes.push(key);
    if (nodes.length) renderer.refresh({ partialGraph: { nodes }, skipIndexation: true, schedule: true });
  }

  function update(): void {
    frame = 0;
    if (isRegionMode(store.get(), renderer.getCamera().ratio)) return resize(null);
    if (!sorted) {
      const points: { key: string; x: number; y: number }[] = [];
      graph.forEachNode((key, node) => {
        if (!node.hidden && node.size > 0) points.push({ key, x: node.x, y: node.y });
      });
      sorted = points.sort((a, b) => a.x - b.x);
    }
    // The camera has rotation disabled. Search only the nearby x range.
    const center = renderer.viewportToGraph(pointer);
    const limit = Math.abs(renderer.viewportToGraph({ x: pointer.x + proximityDistance, y: pointer.y }).x - center.x);
    let lo = 0;
    let hi = sorted.length;
    while (lo < hi) {
      const mid = (lo + hi) >>> 1;
      const candidate = sorted[mid];
      if (candidate && candidate.x < center.x - limit) lo = mid + 1;
      else hi = mid;
    }
    let nearest: string | null = null;
    let distance = limit;
    for (let i = lo; i < sorted.length; i++) {
      const node = sorted[i];
      if (!node || node.x > center.x + limit) break;
      const d = Math.hypot(node.x - center.x, node.y - center.y);
      if (d < distance) {
        distance = d;
        nearest = node.key;
      }
    }
    resize(nearest, limit > 0 ? radius + (radiusNearCursor - radius) * (1 - distance / limit) : radius);
  }

  function clear(): void {
    cancelAnimationFrame(frame);
    frame = 0;
    resize(null);
  }

  container.addEventListener("pointermove", (event) => {
    if (event.pointerType === "touch" || event.buttons) return clear();
    const bounds = container.getBoundingClientRect();
    pointer = { x: event.clientX - bounds.left, y: event.clientY - bounds.top };
    if (!frame) frame = requestAnimationFrame(update);
  });
  container.addEventListener("pointerleave", clear);
  renderer.getCamera().on("updated", clear);
  graph.on("cleared", () => {
    sorted = null;
    clear();
  });
  return (key) => key === active ? activeSize : undefined;
}
