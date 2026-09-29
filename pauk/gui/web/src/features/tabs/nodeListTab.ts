import type { GraphData, PubDetail } from "../../contracts/graph";
import { TAB_LIST_CONFIG } from "../../core/config";
import { t } from "../../core/i18n";
import { renderList, renderListItem } from "../../core/render";
import type { AppState } from "../../core/state";
import type { TabModule } from "./types";

interface NodeLike {
  key: string;
  gx: number;
  gy: number;
}

interface NodeListTabConfig<T extends NodeLike> {
  items(data: GraphData): T[];
  /** Defaults to every item. */
  visible?(item: T, state: AppState, data: GraphData): boolean;
  compare(a: T, b: T): number;
  label(item: T, state: AppState, pubDetails: Map<string, PubDetail>): string;
  /** Right-hand text: pub count, stars, year. */
  meta(item: T, state: AppState): string | undefined;
}

/**
 * Shared list tab for authors, repos and pubs. A row click only sets
 * `store.selection`; map/build.ts flies the camera to it.
 */
export function createNodeListTab<T extends NodeLike>(config: NodeListTabConfig<T>): TabModule {
  return {
    mount(container, store, _renderer, data, pubDetails) {
      const sorted = [...config.items(data)].sort(config.compare);

      // #tab-content is shared by all tabs and not cleared between switches.
      container.replaceChildren();

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
      // Reset on every new query, or the page could point past the end.
      let page = 0;

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
          ? sorted.filter((item) => config.visible?.(item, state, data))
          : sorted;
        const visible = q
          ? shown.filter((item) => config.label(item, state, pubDetails).toLowerCase().includes(q))
          : shown;

        if (visible.length === 0) {
          const empty = document.createElement("div");
          empty.className = "tab-empty";
          empty.textContent = t("tab.noResults", state.lang);
          listEl.replaceChildren(empty);
          paginationEl.replaceChildren();
          return;
        }

        const pageCount = Math.ceil(visible.length / TAB_LIST_CONFIG.pageSize);
        if (page >= pageCount) page = pageCount - 1;
        const start = page * TAB_LIST_CONFIG.pageSize;
        const pageItems = visible.slice(start, start + TAB_LIST_CONFIG.pageSize);

        renderList(listEl, pageItems, (item) =>
          renderListItem({
            label: config.label(item, state, pubDetails),
            meta: config.meta(item, state),
            selected: item.key === selectedKey,
            onClick: () => {
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
