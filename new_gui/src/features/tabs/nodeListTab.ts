import type { GraphData, PubDetail } from "../../contracts/graph";
import { t } from "../../core/i18n";
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
 * строке. Всё остальное (подписка на Store, подсветка выбора, поиск, клик =
 * store.set) у них одинаковое, см. {@link createNodeListTab}.
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
 * не меняются, меняется только то, что в ней выбрано), сверху — поле поиска
 * (простой substring-фильтр по `config.label()`, локальный `query` в
 * замыкании — не в Store, это состояние одного поля ввода, а не приложения),
 * `render()` подписан на весь store ради подсветки выбора независимо от
 * того, кликнули по строке списка или по узлу на карте, а клик по строке
 * только пишет `store.selection` — камерой подлетает к узлу централизованно
 * `map/build.ts::mountReactiveGraph` (см. `flyToSelection`), не сама вкладка.
 *
 * @typeParam T - вид узла (AuthorNode/RepoNode/PubNode).
 * @param config - то, чем эта конкретная вкладка отличается от остальных.
 * @returns {@link TabModule} — готовая к использованию в `TAB_MODULES` (features/tabs/index.ts).
 */
export function createNodeListTab<T extends NodeLike>(config: NodeListTabConfig<T>): TabModule {
  return {
    mount(container, store, _renderer, data, pubDetails) {
      const sorted = [...config.items(data)].sort(config.compare);

      // container.replaceChildren(), а не append() в пустоту — container
      // это #tab-content, общий на все вкладки (features/tabs/index.ts не
      // чистит его сам между переключениями), раньше единственным, что его
      // чистило, был сам renderList() ниже; с появлением searchInput как
      // отдельного узла ПЕРЕД списком это нужно сделать явно самим mount().
      container.replaceChildren();

      const searchInput = document.createElement("input");
      searchInput.type = "search";
      searchInput.className = "tab-search";
      searchInput.placeholder = t("tab.searchPlaceholder", store.get().lang);
      container.append(searchInput);

      const listEl = document.createElement("div");
      container.append(listEl);

      let query = "";

      function render(state: AppState): void {
        searchInput.placeholder = t("tab.searchPlaceholder", state.lang);

        const selectedKey = state.selection?.kind === "node" ? state.selection.key : null;
        const q = query.trim().toLowerCase();
        const visible = q
          ? sorted.filter((item) => config.label(item, state, pubDetails).toLowerCase().includes(q))
          : sorted;

        // Пустой список без объяснения выглядит как сломанная страница, а
        // не как "по этому запросу ничего нет" — раньше здесь после
        // неудачного поиска просто не было вообще ничего под полем ввода.
        if (visible.length === 0) {
          const empty = document.createElement("div");
          empty.className = "tab-empty";
          empty.textContent = t("tab.noResults", state.lang);
          listEl.replaceChildren(empty);
          return;
        }

        renderList(listEl, visible, (item) =>
          renderListItem({
            label: config.label(item, state, pubDetails),
            meta: config.meta(item, state),
            selected: item.key === selectedKey,
            onClick: () => {
              // Камеру двигать не нужно здесь — map/build.ts::mountReactiveGraph
              // сам подлетает к выбранному узлу через flyToSelection на любую
              // смену store.selection, откуда бы она ни пришла.
              store.set({ selection: { kind: "node", key: item.key } });
            },
          }),
        );
      }

      searchInput.addEventListener("input", () => {
        query = searchInput.value;
        render(store.get());
      });

      render(store.get());
      return store.subscribe(render);
    },
  };
}
