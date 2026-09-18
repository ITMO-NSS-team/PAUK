import type { GraphData, PubDetail } from "../../contracts/graph";
import { TAB_LIST_CONFIG } from "../../core/config";
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
  /** Показывать ли узел при текущих фильтрах — по умолчанию все. */
  visible?(item: T, state: AppState): boolean;
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

      // Подпись-раздел "Быстрый поиск"/"Quick Search" (прямая просьба —
      // разделы сайдбара "с подписями") — рисуется самим этим модулем, а
      // не общей разметкой index.html: #tab-content целиком перестраивается
      // здесь при каждой смене вкладки, отдельного статического места под
      // подпись снаружи не нужно.
      const sectionLabel = document.createElement("div");
      sectionLabel.className = "sidebar-section-label";
      container.append(sectionLabel);

      const searchInput = document.createElement("input");
      searchInput.type = "search";
      searchInput.className = "tab-search";
      searchInput.placeholder = t("tab.searchPlaceholder", store.get().lang);
      container.append(searchInput);

      const listEl = document.createElement("div");
      container.append(listEl);

      const paginationEl = document.createElement("div");
      paginationEl.className = "tab-pagination";
      container.append(paginationEl);

      let query = "";
      // Номер текущей страницы (с нуля) — сбрасывается на 0 при каждом
      // новом поисковом запросе (см. ниже): иначе после поиска можно было
      // бы застрять на, скажем, пятой странице совсем другого, гораздо
      // более короткого результата, где такой страницы уже нет.
      let page = 0;

      /**
       * Строит "‹ N / M ›" под списком — только когда страниц больше одной
       * (на коротком результате пагинация просто не нужна, не показываем
       * пустую строку управления ради одной-единственной страницы).
       */
      function renderPagination(pageCount: number, lang: AppState["lang"]): void {
        if (pageCount <= 1) {
          paginationEl.replaceChildren();
          return;
        }

        const prev = document.createElement("button");
        prev.type = "button";
        prev.className = "tab-pagination__button";
        prev.textContent = "‹";
        prev.disabled = page === 0;
        prev.setAttribute("aria-label", t("tab.prevPage", lang));
        prev.addEventListener("click", () => {
          page -= 1;
          render(store.get());
        });

        const status = document.createElement("span");
        status.className = "tab-pagination__status";
        status.textContent = `${page + 1} / ${pageCount}`;

        const next = document.createElement("button");
        next.type = "button";
        next.className = "tab-pagination__button";
        next.textContent = "›";
        next.disabled = page >= pageCount - 1;
        next.setAttribute("aria-label", t("tab.nextPage", lang));
        next.addEventListener("click", () => {
          page += 1;
          render(store.get());
        });

        paginationEl.replaceChildren(prev, status, next);
      }

      function render(state: AppState): void {
        sectionLabel.textContent = t("section.quickSearch", state.lang);
        searchInput.placeholder = t("tab.searchPlaceholder", state.lang);

        const selectedKey = state.selection?.kind === "node" ? state.selection.key : null;
        const q = query.trim().toLowerCase();
        const shown = config.visible
          ? sorted.filter((item) => config.visible?.(item, state))
          : sorted;
        const visible = q
          ? shown.filter((item) => config.label(item, state, pubDetails).toLowerCase().includes(q))
          : shown;

        // Пустой список без объяснения выглядит как сломанная страница, а
        // не как "по этому запросу ничего нет" — раньше здесь после
        // неудачного поиска просто не было вообще ничего под полем ввода.
        if (visible.length === 0) {
          const empty = document.createElement("div");
          empty.className = "tab-empty";
          empty.textContent = t("tab.noResults", state.lang);
          listEl.replaceChildren(empty);
          paginationEl.replaceChildren();
          return;
        }

        const pageCount = Math.ceil(visible.length / TAB_LIST_CONFIG.pageSize);
        // Список мог сократиться (новый запрос, смена языка на фильтр —
        // впрочем, язык тут ни при чём, но сам факт "visible стал короче")
        // так, что текущая страница перестала существовать — откатываемся
        // на последнюю реально существующую, а не показываем пустоту.
        if (page >= pageCount) page = pageCount - 1;
        const start = page * TAB_LIST_CONFIG.pageSize;
        const pageItems = visible.slice(start, start + TAB_LIST_CONFIG.pageSize);

        renderList(listEl, pageItems, (item) =>
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
        renderPagination(pageCount, state.lang);
      }

      searchInput.addEventListener("input", () => {
        query = searchInput.value;
        page = 0;
        render(store.get());
      });

      render(store.get());
      return store.subscribe(render);
    },
  };
}
