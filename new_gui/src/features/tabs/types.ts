import type { Map as MapLibreMap } from "maplibre-gl";
import type { GraphData } from "../../contracts/graph";
import type { SearchDetail } from "../../contracts/search";
import type { AppState, Store } from "../../core/state";

/**
 * Общий контракт для любой вкладки-списка (авторы/репозитории/публикации).
 * app/main.ts не знает ничего про внутренности конкретной вкладки — только
 * про этот интерфейс: смонтировать в container, получить назад функцию
 * размонтирования (unmount). Переключение вкладки — это "unmount текущей,
 * mount новой", а не разрастающаяся ветка if/else внутри одной функции,
 * как было в старом main.js (setTab()).
 *
 * searchDetails нужен не всем вкладкам (только pubs/search — там, где
 * показывается настоящее название публикации, а не её ключ), но передаётся
 * в контракт всем — так же, как и data, из которой каждая вкладка тоже
 * использует только часть.
 */
export interface TabModule {
  mount(
    container: HTMLElement,
    store: Store<AppState>,
    map: MapLibreMap,
    data: GraphData,
    searchDetails: Map<string, SearchDetail>,
  ): () => void;
}
