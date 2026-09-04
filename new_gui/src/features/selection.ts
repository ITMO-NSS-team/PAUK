// Слой "features" — реакция на клики пользователя по карте: превращает их
// в изменения общего состояния (Store) и просит карту подсветить выбранный
// узел. Сама отрисовка (как выглядит подсветка) остаётся в map/build.ts —
// этот файл только решает, ЧТО выбрано, а не КАК это нарисовать.

import type { Map as MapLibreMap, MapMouseEvent } from "maplibre-gl";
import type { AppState, Store } from "../core/state";
import { EDGE_LAYER_ID, NODE_LAYER_ID, setSelectedNode } from "../map/build";

/**
 * Подключает выбор узла/ребра кликом по карте: клик по точке — выбрать
 * узел, клик по линии (если под курсором нет узла) — выбрать ребро, клик
 * по пустому месту — снять выбор. Узел приоритетнее ребра: узлы рисуются
 * поверх линий (см. порядок addLayer в map/build.ts), поэтому клик рядом
 * с пересечением узла и ребра ожидаемо выбирает узел. Курсор меняется на
 * "руку" при наведении на узел — обычная подсказка "тут можно кликнуть".
 *
 * Возвращает функцию отписки (unmount) — снимает все обработчики событий
 * с карты, чтобы при пересборке фичи (например, смене вкладки) не
 * накапливались дублирующиеся слушатели на одной и той же карте.
 */
export function mountSelection(map: MapLibreMap, store: Store<AppState>): () => void {
  function onClick(event: MapMouseEvent): void {
    // queryRenderedFeatures смотрит, что реально нарисовано в точке клика
    // на конкретном слое — так же работает и клик по "пустому месту": там
    // ни в одном из слоёв ничего нет, features будет пустым массивом.
    const nodeFeature = map.queryRenderedFeatures(event.point, { layers: [NODE_LAYER_ID] })[0];
    const nodeKey = nodeFeature?.properties?.key as string | undefined;
    if (nodeKey) {
      store.set({ selection: { kind: "node", key: nodeKey } });
      return;
    }

    const edgeFeature = map.queryRenderedFeatures(event.point, { layers: [EDGE_LAYER_ID] })[0];
    const edgeProps = edgeFeature?.properties as { s?: string; t?: string; w?: number } | undefined;
    if (edgeProps?.s && edgeProps.t && typeof edgeProps.w === "number") {
      store.set({ selection: { kind: "edge", s: edgeProps.s, t: edgeProps.t, w: edgeProps.w } });
      return;
    }

    store.set({ selection: null });
  }

  function onEnterNode(): void {
    map.getCanvas().style.cursor = "pointer";
  }

  function onLeaveNode(): void {
    map.getCanvas().style.cursor = "";
  }

  // Обычный клик по карте (не по конкретному слою) — единая точка входа и
  // для выбора узла/ребра, и для снятия выбора, поэтому не нужен отдельный
  // обработчик "клик по пустому месту".
  map.on("click", onClick);
  map.on("mouseenter", NODE_LAYER_ID, onEnterNode);
  map.on("mouseleave", NODE_LAYER_ID, onLeaveNode);

  // Подписка на Store: как только selection в состоянии меняется (в том
  // числе не из-за клика по карте, а, например, из поиска), карта
  // перерисовывает подсветку — источник правды один (Store), а не два
  // рассинхронизированных состояния (что выбрано на карте и что в Store).
  const unsubscribe = store.subscribe((state) => {
    const key = state.selection?.kind === "node" ? state.selection.key : null;
    setSelectedNode(map, key);
  });

  return () => {
    map.off("click", onClick);
    map.off("mouseenter", NODE_LAYER_ID, onEnterNode);
    map.off("mouseleave", NODE_LAYER_ID, onLeaveNode);
    unsubscribe();
  };
}
