import type Sigma from "sigma";
import type { GraphData, PubDetail, RepoDetail } from "../../contracts/graph";
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
   * @param renderer - Sigma-рендерер (нужен, чтобы подлетать к выбранному узлу через камеру).
   * @param data - данные графа.
   * @param pubDetails - карта деталей публикаций (нужна не всем вкладкам —
   *   только там, где показывается настоящее название публикации, а не
   *   её ключ, — но передаётся в контракт всем, так же как и `data`, из
   *   которой каждая вкладка тоже использует только часть).
   * @param repoDetails - карта описаний/владельцев/ссылок репозиториев (см.
   *   `contracts/graph.ts::RepoDetail`) — сейчас ни одна из смонтированных
   *   вкладок её не читает (нужна была только временно убранной вкладке
   *   "Поиск" для короткого пути на GitHub в `sub` результата), оставлена в
   *   контракте на случай, если поиск вернётся в похожем виде.
   * @returns Функция размонтирования (unmount) — отписывается от Store и снимает обработчики.
   */
  mount(
    container: HTMLElement,
    store: Store<AppState>,
    renderer: Sigma,
    data: GraphData,
    pubDetails: Map<string, PubDetail>,
    repoDetails: Map<string, RepoDetail>,
  ): () => void;
}
