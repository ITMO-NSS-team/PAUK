import type { AuthorDetail, GraphData, PubDetail, RepoDetail } from "../contracts/graph";
import type { SearchHit } from "../contracts/search";
import { SEARCH_CONFIG } from "../core/config";
import { requireElement } from "../core/dom";
import { t } from "../core/i18n";
import { renderList, renderListItem } from "../core/render";
import { TAB_FOR_KIND, type AppState, type Store } from "../core/state";
import { visibleAuthors } from "../map/build";
import { buildSearchIndex, parseDeptHitKey, searchHits } from "./search";

/**
 * Search over all kinds at once. It is the only way to find a department,
 * which has no tab and no clickable node. Opens with the button or "/",
 * works from any screen.
 *
 * @returns Unmount function.
 */
export function mountGlobalSearch(
  store: Store<AppState>,
  data: GraphData,
  pubDetails: Map<string, PubDetail>,
  repoDetails: Map<string, RepoDetail>,
  authorDetails = new Map<string, AuthorDetail>(),
): () => void {
  const trigger = requireElement("global-search-trigger");
  const overlay = requireElement("global-search");
  const input = requireElement("global-search-input") as HTMLInputElement;
  const resultsEl = requireElement("global-search-results");

  // Rebuilt on every open, so it follows the language and the external
  // authors filter.
  function buildIndex(): SearchHit[] {
    const { lang, filters } = store.get();
    const shown = { ...data, authors: visibleAuthors(data, filters) };
    return buildSearchIndex(shown, lang, pubDetails, repoDetails, authorDetails);
  }
  let index = buildIndex();

  function renderTrigger(lang: AppState["lang"]): void {
    const hint = document.createElement("span");
    hint.className = "global-search-trigger__hint";
    hint.textContent = "/";
    trigger.replaceChildren(t("search.trigger", lang), hint);
  }
  renderTrigger(store.get().lang);
  const unsubscribeTrigger = store.subscribe((state) => renderTrigger(state.lang));

  /** @param container - A separate element when a heading must stay above the list. */
  function renderHits(container: HTMLElement, hits: SearchHit[]): void {
    renderList(container, hits, (hit) =>
      renderListItem({
        label: hit.label,
        meta: hit.sub ?? undefined,
        dataKind: hit.kind,
        onClick: () => {
          // Search is also open on the menu, so always switch to the app.
          if (hit.kind === "dept") {
            store.set({ screen: "app", selection: { kind: "dept", id: parseDeptHitKey(hit.key) } });
          } else {
            store.set({
              screen: "app",
              tab: TAB_FOR_KIND[hit.kind],
              selection: { kind: "node", key: hit.key },
            });
          }
          close();
        },
      }),
    );
  }

  function render(): void {
    const query = input.value;

    if (!query.trim()) {
      // Empty query lists the largest departments: they cannot be found any
      // other way without knowing the name.
      const deptById = new Map(data.departments.map((dept) => [dept.id, dept]));
      const deptHits = [...index]
        .filter((hit) => hit.kind === "dept")
        .sort(
          (a, b) =>
            (deptById.get(parseDeptHitKey(b.key))?.n ?? 0) -
            (deptById.get(parseDeptHitKey(a.key))?.n ?? 0),
        )
        .slice(0, SEARCH_CONFIG.resultsLimit);

      const heading = document.createElement("div");
      heading.className = "global-search-hint";
      heading.textContent = t("search.browseDepts", store.get().lang);
      const list = document.createElement("div");
      resultsEl.replaceChildren(heading, list);
      renderHits(list, deptHits);
      return;
    }

    const hits = searchHits(index, query);
    if (hits.length === 0) {
      const empty = document.createElement("div");
      empty.className = "tab-empty";
      empty.textContent = t("tab.noResults", store.get().lang);
      resultsEl.replaceChildren(empty);
      return;
    }

    renderHits(resultsEl, hits.slice(0, SEARCH_CONFIG.resultsLimit));
  }

  function open(): void {
    const lang = store.get().lang;
    index = buildIndex();
    input.placeholder = t("search.placeholder", lang);
    overlay.hidden = false;
    input.value = "";
    render();
    input.focus();
  }

  function close(): void {
    overlay.hidden = true;
  }

  input.addEventListener("input", render);
  trigger.addEventListener("click", open);
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) close();
  });

  /**
   * Enter picks the first result only when focus is in the input; on a result
   * button it clicks that button as usual.
   */
  function onKeydown(event: KeyboardEvent): void {
    if (!overlay.hidden && event.key === "Escape") {
      close();
      return;
    }
    if (!overlay.hidden && event.key === "Enter" && event.target === input) {
      resultsEl.querySelector<HTMLButtonElement>(".tab-list-item")?.click();
      return;
    }
    const target = event.target as HTMLElement;
    const typing = target.tagName === "INPUT" || target.tagName === "TEXTAREA";
    if (overlay.hidden && event.key === "/" && !typing) {
      event.preventDefault();
      open();
    }
  }
  document.addEventListener("keydown", onKeydown);

  return () => {
    document.removeEventListener("keydown", onKeydown);
    unsubscribeTrigger();
  };
}
