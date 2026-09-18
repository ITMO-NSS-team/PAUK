// Слой "features" — реакция на клики пользователя по графу: превращает их
// в изменения общего состояния (Store). Подсветка выбранного узла/ребра —
// целиком в map/build.ts (единый nodeReducer/edgeReducer, см.
// applyGraphStyling) — этот файл только решает, ЧТО выбрано, а не КАК это
// нарисовать.
//
// В отличие от MapLibre-версии, здесь не нужно вручную решать приоритет
// "сначала узел, потом ребро, иначе пусто": Sigma сама различает три
// случая (clickNode/clickEdge/clickStage) через собственный picking-слой —
// клик физически попадает ровно в одно из трёх событий.

import type Sigma from "sigma";
import type {
  Coordinates,
  SigmaEdgeEventPayload,
  SigmaNodeEventPayload,
  SigmaStageEventPayload,
} from "sigma/types";
import { isRegionMode, type AppState, type Store } from "../core/state";

/**
 * Подключает выбор узла/ребра кликом по графу и курсор-подсказку на
 * наведении. `enableEdgeEvents: true` (см. map/build.ts::mountReactiveGraph)
 * обязателен, иначе `clickEdge`/`enterEdge`/`leaveEdge` не приходят вовсе —
 * по умолчанию Sigma эти события не считает (дороже трассировать клики по
 * тонким линиям, чем по кругам).
 *
 * @param renderer - Sigma-рендерер.
 * @param store - Store приложения.
 * @param deptAtViewport - департамент, чей регион сейчас под точкой экрана
 *   (map/regions.ts::mountRegions), или `null`. В режиме регионов любой клик
 *   (по узлу, ребру или пустому месту) выбирает департамент под курсором.
 * @returns Функция отписки (unmount) — снимает все обработчики событий.
 */
export function mountSelection(
  renderer: Sigma,
  store: Store<AppState>,
  deptAtViewport: (point: Coordinates) => number | null = () => null,
): () => void {
  const graph = renderer.getGraph();

  /** Режим регионов: узлы и рёбра не выбираются, клик выбирает регион (core/state.ts::isRegionMode). */
  function regionMode(): boolean {
    return isRegionMode(store.get(), renderer.getCamera().getState().ratio);
  }

  /** Клик в режиме регионов — департамент региона под курсором, либо снять выбор. */
  function selectRegionAt(event: Coordinates): void {
    const dept = deptAtViewport({ x: event.x, y: event.y });
    store.set({ selection: dept === null ? null : { kind: "dept", id: dept } });
  }

  /**
   * Клик по узлу — выбрать его. `node` — это же graphology node id, оно же
   * `node.key` из данных. Узлы-якоря департаментов (map/build.ts::addDeptLabelAnchors)
   * сюда не попадают: у них `size: 0`, кликнуть по ним нельзя.
   */
  function onClickNode({ node, event }: SigmaNodeEventPayload): void {
    if (regionMode()) return selectRegionAt(event);
    store.set({ selection: { kind: "node", key: node } });
  }

  /**
   * Клик по ребру — выбрать его. `edge` — внутренний graphology-ключ ребра
   * (не то же самое, что `s`/`t` из данных), поэтому концы и вес достаются
   * из графа: `graph.extremities()` — те самые `s`/`t`, `weight` — атрибут,
   * записанный при наполнении графа (см. map/build.ts::populateGraph).
   */
  function onClickEdge({ edge, event }: SigmaEdgeEventPayload): void {
    if (regionMode()) return selectRegionAt(event);
    const [s, t] = graph.extremities(edge);
    const w = graph.getEdgeAttribute(edge, "weight") as number;
    store.set({ selection: { kind: "edge", s, t, w } });
  }

  /** Клик по пустому месту холста — регион под курсором в режиме регионов, иначе снять выбор. */
  function onClickStage({ event }: SigmaStageEventPayload): void {
    selectRegionAt(event);
  }

  /** Переключает курсор на "руку" при наведении на узел или ребро — кроме режима регионов, там курсором управляют регионы. */
  function onEnterInteractive(): void {
    if (!regionMode()) renderer.getContainer().style.cursor = "pointer";
  }

  /** Возвращает курсор в обычное состояние, когда наведение уходит с узла/ребра. */
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
