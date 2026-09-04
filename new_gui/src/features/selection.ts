// Слой "features" — реакция на клики пользователя по карте: превращает их
// в изменения общего состояния (Store) и просит карту подсветить выбранный
// узел. Сама отрисовка (как выглядит подсветка) остаётся в map/build.ts —
// этот файл только решает, ЧТО выбрано, а не КАК это нарисовать.

import type { Map as MapLibreMap, MapMouseEvent } from "maplibre-gl";
import type { AppState, Store } from "../core/state";
import { NODE_LAYER_ID, setSelectedNode } from "../map/build";

/**
 * Подключает выбор узла кликом по карте: клик по точке — выбрать её и
 * записать в Store, клик по пустому месту — снять выбор. Курсор меняется
 * на "руку" при наведении на узел — обычная подсказка "тут можно кликнуть".
 *
 * Возвращает функцию отписки (unmount) — снимает все обработчики событий
 * с карты, чтобы при пересборке фичи (например, смене вкладки) не
 * накапливались дублирующиеся слушатели на одной и той же карте.
 */
export function mountSelection(map: MapLibreMap, store: Store<AppState>): () => void {
  function onClick(event: MapMouseEvent): void {
    // queryRenderedFeatures смотрит, что реально нарисовано в точке клика
    // на конкретном слое — так же работает и клик по "пустому месту": там
    // в слое узлов ничего нет, features будет пустым массивом.
    const features = map.queryRenderedFeatures(event.point, { layers: [NODE_LAYER_ID] });
    const feature = features[0];
    const key = feature?.properties?.key as string | undefined;

    if (!key) {
      store.set({ selection: null });
      return;
    }
    store.set({ selection: { kind: "node", key } });
  }

  function onEnterNode(): void {
    map.getCanvas().style.cursor = "pointer";
  }

  function onLeaveNode(): void {
    map.getCanvas().style.cursor = "";
  }

  // Обычный клик по карте (не по конкретному слою) — единая точка входа и
  // для выбора узла, и для снятия выбора, поэтому не нужен отдельный
  // обработчик "клик по пустому месту".
  map.on("click", onClick);
  map.on("mouseenter", NODE_LAYER_ID, onEnterNode);
  map.on("mouseleave", NODE_LAYER_ID, onLeaveNode);

  // Подписка на Store: как только selection в состоянии меняется (в том
  // числе не из-за клика по карте, а, например, из будущего поиска),
  // карта перерисовывает подсветку — источник правды один (Store), а не
  // два рассинхронизированных состояния (что выбрано на карте и что в Store).
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
