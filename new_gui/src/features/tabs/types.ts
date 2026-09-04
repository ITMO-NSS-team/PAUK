import type { Map as MapLibreMap } from "maplibre-gl";
import type { GraphData } from "../../contracts/graph";
import type { AppState, Store } from "../../core/state";

/**
 * Общий контракт для любой вкладки-списка (авторы/репозитории/публикации).
 * app/main.ts не знает ничего про внутренности конкретной вкладки — только
 * про этот интерфейс: смонтировать в container, получить назад функцию
 * размонтирования (unmount). Переключение вкладки — это "unmount текущей,
 * mount новой", а не разрастающаяся ветка if/else внутри одной функции,
 * как было в старом main.js (setTab()).
 */
export interface TabModule {
  mount(container: HTMLElement, store: Store<AppState>, map: MapLibreMap, data: GraphData): () => void;
}
