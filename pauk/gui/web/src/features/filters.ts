import type { GraphData } from "../contracts/graph";
import { FILTER_CONFIG } from "../core/config";
import { isolatedAuthors } from "../map/build";
import { requireElement } from "../core/dom";
import { t, type Lang } from "../core/i18n";
import type { AppState, Store } from "../core/state";

interface FilterRowOptions {
  label: string;
  min: number;
  max: number;
  /** Defaults to 1; fractional for continuous values like camera ratio. */
  step?: number;
  value: number;
  /** Debounced: applying a filter rebuilds the whole graph. */
  onChange: (value: number) => void;
}

function buildFilterRow(options: FilterRowOptions): HTMLElement {
  const row = document.createElement("label");
  row.className = "filter-row";

  const label = document.createElement("span");
  label.className = "filter-row__label";
  label.textContent = options.label;

  const input = document.createElement("input");
  input.type = "range";
  input.min = String(options.min);
  input.max = String(options.max);
  input.step = String(options.step ?? 1);
  input.value = String(options.value);

  const value = document.createElement("span");
  value.className = "filter-row__value";
  value.textContent = String(options.value);

  // The value label updates on every tick, onChange is debounced: a graph
  // rebuild per pixel of dragging lags.
  let debounceTimer: ReturnType<typeof setTimeout> | undefined;
  input.addEventListener("input", () => {
    value.textContent = input.value;
    const parsed = Number(input.value);
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => options.onChange(parsed), FILTER_CONFIG.debounceMs);
  });

  row.append(label, input, value);
  return row;
}

interface CheckboxRowOptions {
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}

function buildCheckboxRow(options: CheckboxRowOptions): HTMLElement {
  const row = document.createElement("label");
  row.className = "filter-row";

  const input = document.createElement("input");
  input.type = "checkbox";
  input.checked = options.checked;
  input.addEventListener("change", () => options.onChange(input.checked));

  const label = document.createElement("span");
  label.className = "filter-row__label";
  label.textContent = options.label;

  row.append(input, label);
  return row;
}

/**
 * Rebuilds the rows only on tab or language change, not on every store update.
 *
 * @returns Unmount function.
 */
export function mountFilters(store: Store<AppState>): () => void {
  const container = requireElement("filter-bar");
  const sectionLabel = requireElement("filters-section-label");

  let prevTab: AppState["tab"] | null = null;
  let prevLang: Lang | null = null;

  function setFilter(patch: Partial<AppState["filters"]>): void {
    store.set({ filters: { ...store.get().filters, ...patch } });
  }

  function render(state: AppState): void {
    if (state.tab === prevTab && state.lang === prevLang) return;
    prevTab = state.tab;
    prevLang = state.lang;

    const { lang, filters } = state;
    sectionLabel.textContent = t("section.filters", lang);
    const rows: HTMLElement[] = [
      buildFilterRow({
        label: t("filter.edgeZoom", lang),
        min: FILTER_CONFIG.edgeZoom.min,
        max: FILTER_CONFIG.edgeZoom.max,
        step: FILTER_CONFIG.edgeZoom.step,
        value: filters.edgeZoomThreshold,
        onChange: (value) => setFilter({ edgeZoomThreshold: value }),
      }),
    ];

    if (state.tab === 1) {
      rows.push(
        buildFilterRow({
          label: t("filter.coauth", lang),
          min: FILTER_CONFIG.coauth.min,
          max: FILTER_CONFIG.coauth.max,
          value: filters.minCoauth,
          onChange: (value) => setFilter({ minCoauth: value }),
        }),
        buildCheckboxRow({
          label: t("filter.showNoDept", lang),
          checked: filters.showNoDeptAuthors,
          onChange: (checked) => setFilter({ showNoDeptAuthors: checked }),
        }),
        buildCheckboxRow({
          label: t("filter.showExternal", lang),
          checked: filters.showExternalAuthors,
          onChange: (checked) => setFilter({ showExternalAuthors: checked }),
        }),
        buildCheckboxRow({
          label: t("filter.showIsolated", lang),
          checked: filters.showIsolatedAuthors,
          onChange: (checked) => setFilter({ showIsolatedAuthors: checked }),
        }),
      );
    } else if (state.tab === 3) {
      rows.push(
        buildFilterRow({
          label: t("filter.sharedAuthors", lang),
          min: FILTER_CONFIG.sharedAuthors.min,
          max: FILTER_CONFIG.sharedAuthors.max,
          value: filters.minSharedAuthors,
          onChange: (value) => setFilter({ minSharedAuthors: value }),
        }),
        buildFilterRow({
          label: t("filter.yearMax", lang),
          min: FILTER_CONFIG.year.min,
          max: FILTER_CONFIG.year.max,
          value: filters.yearMax,
          onChange: (value) => setFilter({ yearMax: value }),
        }),
        buildCheckboxRow({
          label: t("filter.showNoDept", lang),
          checked: filters.showNoDeptPubs,
          onChange: (checked) => setFilter({ showNoDeptPubs: checked }),
        }),
      );
    }

    // Region toggle is per tab, region thresholds are shared.
    rows.push(
      buildCheckboxRow({
        label: t("filter.showRegions", lang),
        checked: filters.showRegions[state.tab],
        onChange: (checked) =>
          setFilter({ showRegions: { ...store.get().filters.showRegions, [state.tab]: checked } }),
      }),
      buildFilterRow({
        label: t("filter.regionZoom", lang),
        min: FILTER_CONFIG.regionZoom.min,
        max: FILTER_CONFIG.regionZoom.max,
        step: FILTER_CONFIG.regionZoom.step,
        value: filters.regionZoomThreshold,
        onChange: (value) => setFilter({ regionZoomThreshold: value }),
      }),
      buildFilterRow({
        label: t("filter.regionMinNodes", lang),
        min: FILTER_CONFIG.regionMinNodes.min,
        max: FILTER_CONFIG.regionMinNodes.max,
        value: filters.regionMinNodes,
        onChange: (value) => setFilter({ regionMinNodes: value }),
      }),
    );

    container.hidden = false;
    container.replaceChildren(...rows);
  }

  render(store.get());
  return store.subscribe(render);
}

/**
 * Turns on the filter that hides the selected author (external, isolated),
 * e.g. after a link from a publication card. Must subscribe before
 * map/build.ts::mountReactiveGraph, which drops selections missing from the graph.
 *
 * @returns Unmount function.
 */
export function mountHiddenAuthorReveal(store: Store<AppState>, data: GraphData): () => void {
  const external = new Set(data.authors.filter((a) => a.is_itmo === false).map((a) => a.key));
  return store.subscribe((state) => {
    const { selection, filters } = state;
    if (selection?.kind !== "node") return;
    const needExternal = !filters.showExternalAuthors && external.has(selection.key);
    // Isolated status depends on whether external authors are shown.
    const withExternal = filters.showExternalAuthors || needExternal;
    const needIsolated =
      !filters.showIsolatedAuthors && isolatedAuthors(data, withExternal).has(selection.key);
    if (needExternal || needIsolated) {
      store.set({
        filters: {
          ...filters,
          showExternalAuthors: filters.showExternalAuthors || needExternal,
          showIsolatedAuthors: filters.showIsolatedAuthors || needIsolated,
        },
      });
    }
  });
}
