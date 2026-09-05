import type { Map as MapLibreMap } from "maplibre-gl";
import type { GraphData } from "../../contracts/graph";
import type { SearchDetail } from "../../contracts/search";
import type { AppState, Store } from "../../core/state";

/**
 * Общий контракт для любой вкладки-списка (авторы/репозитории/публикации/
 * поиск). `app/main.ts` и `features/tabs/index.ts` не знают ничего про
 * внутренности конкретной вкладки — только про этот интерфейс: смонтировать
 * в `container`, получить назад функцию размонтирования (`unmount`).
 * Переключение вкладки — это "unmount текущей, mount новой", а не
 * разрастающаяся ветка `if/else` внутри одной функции, как было в старом
 * `main.js` (`setTab()`).
 */
export interface TabModule {
  /**
   * Монтирует вкладку в `container`: рисует список, подписывается на Store
   * и на клики по своим элементам.
   *
   * @param container - DOM-элемент, куда вкладка рисует свою разметку (обычно `#tab-content`).
   * @param store - Store приложения.
   * @param map - экземпляр карты MapLibre (нужен, чтобы подлетать к выбранному узлу).
   * @param data - данные графа.
   * @param searchDetails - карта деталей публикаций (нужна не всем вкладкам —
   *   только там, где показывается настоящее название публикации, а не
   *   её ключ, — но передаётся в контракт всем, так же как и `data`, из
   *   которой каждая вкладка тоже использует только часть).
   * @returns Функция размонтирования (unmount) — отписывается от Store и снимает обработчики.
   */
  mount(
    container: HTMLElement,
    store: Store<AppState>,
    map: MapLibreMap,
    data: GraphData,
    searchDetails: Map<string, SearchDetail>,
  ): () => void;
}
