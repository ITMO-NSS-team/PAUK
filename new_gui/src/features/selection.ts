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
import type { AppState, Store } from "../core/state";

/**
 * Подключает выбор узла/ребра кликом по графу и курсор-подсказку на
 * наведении. `enableEdgeEvents: true` (см. map/build.ts::mountReactiveGraph)
 * обязателен, иначе `clickEdge`/`enterEdge`/`leaveEdge` не приходят вовсе —
 * по умолчанию Sigma эти события не считает (дороже трассировать клики по
 * тонким линиям, чем по кругам).
 *
 * @param renderer - Sigma-рендерер.
 * @param store - Store приложения.
 * @returns Функция отписки (unmount) — снимает все обработчики событий.
 */
export function mountSelection(renderer: Sigma, store: Store<AppState>): () => void {
  const graph = renderer.getGraph();

  /**
   * Клик по узлу — выбрать его. `node` — это же graphology node id, оно же
   * `node.key` из данных. Узлы-якоря подписей департаментов (см.
   * map/build.ts::addDeptLabelAnchors) сюда не попадают: у них `size: 0`,
   * кликнуть по ним нельзя, как и в старом GUI (там подписи департаментов
   * были чистым текстом на пассивном canvas-оверлее) — выбор департамента
   * остаётся доступен через поиск.
   */
  function onClickNode({ node }: { node: string }): void {
    store.set({ selection: { kind: "node", key: node } });
  }

  /**
   * Клик по ребру — выбрать его. `edge` — внутренний graphology-ключ ребра
   * (не то же самое, что `s`/`t` из данных), поэтому концы и вес достаются
   * из графа: `graph.extremities()` — те самые `s`/`t`, `weight` — атрибут,
   * записанный при наполнении графа (см. map/build.ts::populateGraph).
   */
  function onClickEdge({ edge }: { edge: string }): void {
    const [s, t] = graph.extremities(edge);
    const w = graph.getEdgeAttribute(edge, "weight") as number;
    store.set({ selection: { kind: "edge", s, t, w } });
  }

  /** Клик по пустому месту холста — снять выбор. */
  function onClickStage(): void {
    store.set({ selection: null });
  }

  /** Переключает курсор на "руку" — вызывается при наведении на узел или на ребро. */
  function onEnterInteractive(): void {
    renderer.getContainer().style.cursor = "pointer";
  }

  /** Возвращает курсор в обычное состояние — вызывается, когда наведение уходит с узла/ребра. */
  function onLeaveInteractive(): void {
    renderer.getContainer().style.cursor = "";
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
