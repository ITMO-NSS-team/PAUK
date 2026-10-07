import type Sigma from "sigma";
import type {
  Coordinates,
  SigmaEdgeEventPayload,
  SigmaNodeEventPayload,
  SigmaStageEventPayload,
} from "sigma/types";
import { isRegionMode, type AppState, type Store } from "../core/state";

/**
 * Needs `enableEdgeEvents: true` on the renderer, otherwise edge events never fire.
 *
 * @param deptAtViewport - Department whose region is under the point. In
 *   region mode every click selects that department.
 * @returns Unmount function.
 */
export function mountSelection(
  renderer: Sigma,
  store: Store<AppState>,
  deptAtViewport: (point: Coordinates) => number | null = () => null,
): () => void {
  const graph = renderer.getGraph();

  function regionMode(): boolean {
    return isRegionMode(store.get(), renderer.getCamera().getState().ratio);
  }

  function selectRegionAt(event: Coordinates): void {
    const dept = deptAtViewport({ x: event.x, y: event.y });
    store.set({ selection: dept === null ? null : { kind: "dept", id: dept } });
  }

  function onClickNode({ node, event }: SigmaNodeEventPayload): void {
    if (regionMode()) return selectRegionAt(event);
    store.set({ selection: { kind: "node", key: node } });
  }

  /** `edge` is a graphology key, so the endpoints come from the graph. */
  function onClickEdge({ edge, event }: SigmaEdgeEventPayload): void {
    if (regionMode()) return selectRegionAt(event);
    const [s, t] = graph.extremities(edge);
    const w = graph.getEdgeAttribute(edge, "weight") as number;
    store.set({ selection: { kind: "edge", s, t, w } });
  }

  function onClickStage({ event }: SigmaStageEventPayload): void {
    selectRegionAt(event);
  }

  /** In region mode the regions own the cursor. */
  function onEnterInteractive(): void {
    if (!regionMode()) renderer.getContainer().style.cursor = "pointer";
  }

  function onLeaveInteractive(): void {
    if (!regionMode()) renderer.getContainer().style.cursor = "";
  }

  renderer.on("clickNode", onClickNode);
  renderer.on("clickEdge", onClickEdge);
  renderer.on("clickStage", onClickStage);
  renderer.on("enterNode", onEnterInteractive);
  renderer.on("leaveNode", onLeaveInteractive);
  renderer.on("enterEdge", onEnterInteractive);
  renderer.on("leaveEdge", onLeaveInteractive);

  return () => {
    renderer.off("clickNode", onClickNode);
    renderer.off("clickEdge", onClickEdge);
    renderer.off("clickStage", onClickStage);
    renderer.off("enterNode", onEnterInteractive);
    renderer.off("leaveNode", onLeaveInteractive);
    renderer.off("enterEdge", onEnterInteractive);
    renderer.off("leaveEdge", onLeaveInteractive);
  };
}
