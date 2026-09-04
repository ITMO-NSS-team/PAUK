// Слой "features" — реакция на клики пользователя по карте: превращает их
// в изменения общего состояния (Store) и просит карту подсветить выбранный
// узел. Сама отрисовка (как выглядит подсветка) остаётся в map/build.ts —
// этот файл только решает, ЧТО выбрано, а не КАК это нарисовать.

import type { Map as MapLibreMap, MapMouseEvent } from "maplibre-gl";
import type { AppState, Store } from "../core/state";
import { EDGE_HIT_LAYER_ID, NODE_LAYER_ID, setSelectedNode } from "../map/build";

/**
 * Подключает выбор узла/ребра кликом по карте: клик по точке — выбрать
 * узел, клик рядом с линией (в пределах невидимого широкого EDGE_HIT_LAYER_ID
 * из map/build.ts, а не видимой тонкой линии) — выбрать ребро, клик по
 * пустому месту — снять выбор. Узел проверяется первым и безусловно
 * приоритетнее ребра: даже там, где широкая область клика ребра
 * перекрывает узел, клик по самому узлу должен выбирать именно узел.
 * Курсор меняется на "руку" при наведении на узел ИЛИ на область ребра —
 * без этого непонятно, что тонкую линию вообще можно выбрать.
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

    const edgeFeature = map.queryRenderedFeatures(event.point, { layers: [EDGE_HIT_LAYER_ID] })[0];
    const edgeProps = edgeFeature?.properties as { s?: string; t?: string; w?: number } | undefined;
    if (edgeProps?.s && edgeProps.t && typeof edgeProps.w === "number") {
      store.set({ selection: { kind: "edge", s: edgeProps.s, t: edgeProps.t, w: edgeProps.w } });
      return;
    }

    store.set({ selection: null });
  }

  function onEnterInteractive(): void {
    map.getCanvas().style.cursor = "pointer";
  }

  function onLeaveInteractive(): void {
    map.getCanvas().style.cursor = "";
  }

  // Обычный клик по карте (не по конкретному слою) — единая точка входа и
  // для выбора узла/ребра, и для снятия выбора, поэтому не нужен отдельный
  // обработчик "клик по пустому месту". Курсор — один и тот же обработчик
  // на оба слоя, поведение при наведении одинаковое что на узел, что на ребро.
  map.on("click", onClick);
  map.on("mouseenter", NODE_LAYER_ID, onEnterInteractive);
  map.on("mouseleave", NODE_LAYER_ID, onLeaveInteractive);
  map.on("mouseenter", EDGE_HIT_LAYER_ID, onEnterInteractive);
  map.on("mouseleave", EDGE_HIT_LAYER_ID, onLeaveInteractive);

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
    map.off("mouseenter", NODE_LAYER_ID, onEnterInteractive);
    map.off("mouseleave", NODE_LAYER_ID, onLeaveInteractive);
    map.off("mouseenter", EDGE_HIT_LAYER_ID, onEnterInteractive);
    map.off("mouseleave", EDGE_HIT_LAYER_ID, onLeaveInteractive);
    unsubscribe();
  };
}
