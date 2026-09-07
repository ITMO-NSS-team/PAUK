import type { GraphData, PubDetail } from "../../contracts/graph";
import { renderList, renderListItem } from "../../core/render";
import type { AppState } from "../../core/state";
import type { TabModule } from "./types";

/** Минимум, который нужен фабрике от узла — есть у AuthorNode/RepoNode/PubNode. */
interface NodeLike {
  key: string;
  gx: number;
  gy: number;
}

/**
 * То, чем отличаются друг от друга вкладки-списки (авторы/репозитории/
 * публикации) — источник данных, порядок сортировки и что показать в
 * строке. Всё остальное (подписка на Store, подсветка выбора, клик =
 * store.set + flyTo) у них одинаковое, см. {@link createNodeListTab}.
 */
interface NodeListTabConfig<T extends NodeLike> {
  /** Достаёт список узлов этой вкладки из полного GraphData. */
  items(data: GraphData): T[];
  /** Компаратор для Array.prototype.sort — порядок вкладки. */
  compare(a: T, b: T): number;
  /** Основной текст строки списка. */
  label(item: T, state: AppState, pubDetails: Map<string, PubDetail>): string;
  /** Второстепенный текст строки справа (число публикаций, звёзды, год и т.п.). */
  meta(item: T, state: AppState): string | undefined;
}

/**
 * Общая реализация вкладки-списка одного вида узлов графа — authorsTab,
 * reposTab и pubsTab (`features/tabs/{authors,repos,pubs}.ts`) отличаются
 * только `NodeListTabConfig`, который им передают, остальное код один в
 * один: список сортируется один раз при монтировании (сами данные вкладки
 * не меняются, меняется только то, что в ней выбрано), `render()`
 * подписан на весь store ради подсветки выбора независимо от того, кликнули
 * по строке списка или по узлу на карте, а клик по строке пишет
 * `store.selection` и подлетает к узлу на карте, не меняя zoom.
 *
 * @typeParam T - вид узла (AuthorNode/RepoNode/PubNode).
 * @param config - то, чем эта конкретная вкладка отличается от остальных.
 * @returns {@link TabModule} — готовая к использованию в `TAB_MODULES` (features/tabs/index.ts).
 */
export function createNodeListTab<T extends NodeLike>(config: NodeListTabConfig<T>): TabModule {
  return {
    mount(container, store, map, data, pubDetails) {
      const sorted = [...config.items(data)].sort(config.compare);

      function render(state: AppState): void {
        const selectedKey = state.selection?.kind === "node" ? state.selection.key : null;

        renderList(container, sorted, (item) =>
          renderListItem({
            label: config.label(item, state, pubDetails),
            meta: config.meta(item, state),
            selected: item.key === selectedKey,
            onClick: () => {
              store.set({ selection: { kind: "node", key: item.key } });
              map.flyTo({ center: [item.gx, item.gy] });
            },
          }),
        );
      }

      render(store.get());
      return store.subscribe(render);
    },
  };
}
