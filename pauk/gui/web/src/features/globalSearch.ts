import type { AuthorDetail, GraphData, PubDetail, RepoDetail } from "../contracts/graph";
import type { SearchHit } from "../contracts/search";
import { SEARCH_CONFIG } from "../core/config";
import { requireElement } from "../core/dom";
import { t, type LocaleKey } from "../core/i18n";
import { renderList, renderListItem } from "../core/render";
import { TAB_FOR_KIND, type AppState, type Store } from "../core/state";
import { buildSearchIndex, parseDeptHitKey, searchHits } from "./search";

const GROUP_PREVIEW_LIMIT = 3;

const GROUP_TITLES = {
  author: "tab.authors",
  repo: "tab.repos",
  pub: "tab.pubs",
  dept: "search.browseDepts",
} satisfies Record<SearchHit["kind"], LocaleKey>;

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

  function buildIndex(): SearchHit[] {
    return buildSearchIndex(data, store.get().lang, pubDetails, repoDetails, authorDetails);
  }
  let index = buildIndex();
  let indexedLang = store.get().lang;
  let detailVersion = `${pubDetails.size}/${repoDetails.size}/${authorDetails.size}`;

  function renderTrigger(lang: AppState["lang"]): void {
    const hint = document.createElement("span");
    hint.className = "global-search-trigger__hint";
    hint.textContent = "/";
    trigger.replaceChildren(t("search.trigger", lang), hint);
  }
  renderTrigger(store.get().lang);
  const unsubscribeTrigger = store.subscribe((state) => {
    renderTrigger(state.lang);
    const nextVersion = `${pubDetails.size}/${repoDetails.size}/${authorDetails.size}`;
    if (state.lang !== indexedLang || nextVersion !== detailVersion) {
      indexedLang = state.lang;
      detailVersion = nextVersion;
      index = buildIndex();
      if (!overlay.hidden) render();
    }
  });

  function renderHits(container: HTMLElement, hits: SearchHit[]): void {
    renderList(container, hits, (hit) =>
      renderListItem({
        label: hit.label,
        meta: hit.sub ?? undefined,
        dataKind: hit.kind,
        onClick: () => {
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

  const expandedGroups = new Set<SearchHit["kind"]>();

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

    const groups = new Map<SearchHit["kind"], SearchHit[]>();
    for (const hit of hits) {
      const group = groups.get(hit.kind) ?? [];
      if (group.length < SEARCH_CONFIG.resultsLimit) group.push(hit);
      groups.set(hit.kind, group);
    }

    const lang = store.get().lang;
    resultsEl.replaceChildren(...Array.from(groups, ([kind, group]) => {
      const section = document.createElement("section");
      section.className = "global-search-group";
      const heading = document.createElement("h2");
      heading.className = "global-search-hint";
      heading.textContent = t(GROUP_TITLES[kind], lang);
      const list = document.createElement("div");
      renderHits(list, group.slice(0, GROUP_PREVIEW_LIMIT));
      section.append(heading, list);
      const remaining = group.slice(GROUP_PREVIEW_LIMIT);
      if (remaining.length) {
        const extra = document.createElement("div");
        renderHits(extra, remaining);
        const toggle = document.createElement("button");
        toggle.type = "button";
        toggle.className = "global-search-more";

        function syncGroup(): void {
          const expanded = expandedGroups.has(kind);
          extra.hidden = !expanded;
          toggle.textContent = t(expanded ? "list.showLess" : "list.showMore", lang);
          toggle.setAttribute("aria-expanded", String(expanded));
        }

        toggle.addEventListener("click", () => {
          if (expandedGroups.has(kind)) expandedGroups.delete(kind);
          else expandedGroups.add(kind);
          syncGroup();
        });
        syncGroup();
        section.append(extra, toggle);
      }
      return section;
    }));
    resultsEl.scrollTop = 0;
  }

  function open(): void {
    const lang = store.get().lang;
    index = buildIndex();
    input.placeholder = t("search.placeholder", lang);
    overlay.hidden = false;
    input.value = "";
    expandedGroups.clear();
    render();
    input.focus();
  }

  function close(): void {
    overlay.hidden = true;
  }

  input.addEventListener("input", () => {
    expandedGroups.clear();
    render();
  });
  trigger.addEventListener("click", open);
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) close();
  });

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
