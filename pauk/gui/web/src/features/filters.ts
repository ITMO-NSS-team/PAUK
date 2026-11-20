import type { GraphData } from "../contracts/graph";
import { FILTER_CONFIG } from "../core/config";
import { isolatedAuthors } from "../map/build";
import { requireElement } from "../core/dom";
import { t, type Lang } from "../core/i18n";
import type { AppState, Store } from "../core/state";

type Filters = AppState["filters"];

interface FilterControl {
  row: HTMLElement;
  sync: (filters: Filters) => void;
  dispose: () => void;
}

interface FilterRowOptions {
  label: string;
  min: number;
  max: number;
  /** Defaults to 1; fractional for continuous values like camera ratio. */
  step?: number;
  value: (filters: Filters) => number;
  /** Debounced: applying a filter rebuilds the whole graph. */
  onChange: (value: number) => void;
}

function buildFilterRow(options: FilterRowOptions, filters: Filters): FilterControl {
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
  let appliedValue = options.value(filters);
  input.value = String(appliedValue);

  const value = document.createElement("span");
  value.className = "filter-row__value";
  value.textContent = String(appliedValue);

  // The value label updates on every tick, onChange is debounced: a graph
  // rebuild per pixel of dragging lags.
  let debounceTimer: ReturnType<typeof setTimeout> | undefined;
  // The thumb tilts toward the drag direction, see index.html.
  let lastValue = appliedValue;
  function onPointerDown(): void {
    delete input.dataset.tilt;
  }
  function onInput(): void {
    value.textContent = input.value;
    const parsed = Number(input.value);
    if (parsed !== lastValue) input.dataset.tilt = parsed < lastValue ? "left" : "right";
    lastValue = parsed;
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => {
      debounceTimer = undefined;
      options.onChange(parsed);
    }, FILTER_CONFIG.debounceMs);
  }
  input.addEventListener("pointerdown", onPointerDown);
  input.addEventListener("input", onInput);

  row.append(label, input, value);
  return {
    row,
    sync(nextFilters) {
      const next = options.value(nextFilters);
      // Other fields and lazy details must not interrupt an in-progress drag.
      if (next === appliedValue) return;
      appliedValue = next;
      clearTimeout(debounceTimer);
      debounceTimer = undefined;
      input.value = String(next);
      value.textContent = String(next);
      lastValue = next;
      delete input.dataset.tilt;
    },
    dispose() {
      clearTimeout(debounceTimer);
      input.removeEventListener("pointerdown", onPointerDown);
      input.removeEventListener("input", onInput);
    },
  };
}

interface CheckboxRowOptions {
  label: string;
  checked: (filters: Filters) => boolean;
  onChange: (checked: boolean) => void;
}

function buildCheckboxRow(options: CheckboxRowOptions, filters: Filters): FilterControl {
  const row = document.createElement("label");
  row.className = "filter-row";

  const input = document.createElement("input");
  input.type = "checkbox";
  input.checked = options.checked(filters);
  function onChange(): void {
    options.onChange(input.checked);
  }
  input.addEventListener("change", onChange);

  const label = document.createElement("span");
  label.className = "filter-row__label";
  label.textContent = options.label;

  row.append(input, label);
  return {
    row,
    sync(nextFilters) {
      input.checked = options.checked(nextFilters);
    },
    dispose() {
      input.removeEventListener("change", onChange);
    },
  };
}

/**
 * Rebuilds the rows only on tab or language change, discarding pending edits.
 * Other updates synchronize existing controls without interrupting other fields.
 *
 * @returns Unmount function.
 */
export function mountFilters(store: Store<AppState>): () => void {
  const container = requireElement("filter-bar");
  const sectionLabel = requireElement("filters-section-label");

  let prevTab: AppState["tab"] | null = null;
  let prevLang: Lang | null = null;
  let controls: FilterControl[] = [];

  function setFilter(patch: Partial<AppState["filters"]>): void {
    store.set({ filters: { ...store.get().filters, ...patch } });
  }

  function render(state: AppState): void {
    if (state.tab === prevTab && state.lang === prevLang) {
      controls.forEach((control) => control.sync(state.filters));
      return;
    }
    controls.forEach((control) => control.dispose());
    prevTab = state.tab;
    prevLang = state.lang;

    const { lang, filters } = state;
    const rangeRow = (options: FilterRowOptions) => buildFilterRow(options, filters);
    const checkboxRow = (options: CheckboxRowOptions) => buildCheckboxRow(options, filters);
    sectionLabel.textContent = t("section.filters", lang);
    const rows: FilterControl[] = [
      rangeRow({
        label: t("filter.edgeZoom", lang),
        min: FILTER_CONFIG.edgeZoom.min,
        max: FILTER_CONFIG.edgeZoom.max,
        step: FILTER_CONFIG.edgeZoom.step,
        value: (filters) => filters.edgeZoomThreshold,
        onChange: (value) => setFilter({ edgeZoomThreshold: value }),
      }),
    ];

    if (state.tab === 1) {
      rows.push(
        rangeRow({
          label: t("filter.coauth", lang),
          min: FILTER_CONFIG.coauth.min,
          max: FILTER_CONFIG.coauth.max,
          value: (filters) => filters.minCoauth,
          onChange: (value) => setFilter({ minCoauth: value }),
        }),
        checkboxRow({
          label: t("filter.showNoDept", lang),
          checked: (filters) => filters.showNoDeptAuthors,
          onChange: (checked) => setFilter({ showNoDeptAuthors: checked }),
        }),
        checkboxRow({
          label: t("filter.showExternal", lang),
          checked: (filters) => filters.showExternalAuthors,
          onChange: (checked) => setFilter({ showExternalAuthors: checked }),
        }),
        checkboxRow({
          label: t("filter.showIsolated", lang),
          checked: (filters) => filters.showIsolatedAuthors,
          onChange: (checked) => setFilter({ showIsolatedAuthors: checked }),
        }),
      );
    } else if (state.tab === 3) {
      rows.push(
        rangeRow({
          label: t("filter.sharedAuthors", lang),
          min: FILTER_CONFIG.sharedAuthors.min,
          max: FILTER_CONFIG.sharedAuthors.max,
          value: (filters) => filters.minSharedAuthors,
          onChange: (value) => setFilter({ minSharedAuthors: value }),
        }),
        rangeRow({
          label: t("filter.yearMax", lang),
          min: FILTER_CONFIG.year.min,
          max: FILTER_CONFIG.year.max,
          value: (filters) => filters.yearMax,
          onChange: (value) => setFilter({ yearMax: value }),
        }),
        checkboxRow({
          label: t("filter.showNoDept", lang),
          checked: (filters) => filters.showNoDeptPubs,
          onChange: (checked) => setFilter({ showNoDeptPubs: checked }),
        }),
      );
    }

    // Region toggle is per tab, region thresholds are shared.
    rows.push(
      checkboxRow({
        label: t("filter.showRegions", lang),
        checked: (filters) => filters.showRegions[state.tab],
        onChange: (checked) =>
          setFilter({ showRegions: { ...store.get().filters.showRegions, [state.tab]: checked } }),
      }),
      rangeRow({
        label: t("filter.regionZoom", lang),
        min: FILTER_CONFIG.regionZoom.min,
        max: FILTER_CONFIG.regionZoom.max,
        step: FILTER_CONFIG.regionZoom.step,
        value: (filters) => filters.regionZoomThreshold,
        onChange: (value) => setFilter({ regionZoomThreshold: value }),
      }),
      rangeRow({
        label: t("filter.regionMinNodes", lang),
        min: FILTER_CONFIG.regionMinNodes.min,
        max: FILTER_CONFIG.regionMinNodes.max,
        value: (filters) => filters.regionMinNodes,
        onChange: (value) => setFilter({ regionMinNodes: value }),
      }),
    );

    controls = rows;
    container.hidden = false;
    container.replaceChildren(...controls.map((control) => control.row));
  }

  render(store.get());
  const unsubscribe = store.subscribe(render);
  return () => {
    unsubscribe();
    controls.forEach((control) => control.dispose());
  };
}

/**
 * Reveals a hidden author (external, isolated) on node-selection/navigation
 * transitions, not on explicit filter changes. Must subscribe before
 * map/build.ts::mountReactiveGraph, which drops selections missing from the graph.
 *
 * @returns Unmount function.
 */
export function mountHiddenAuthorReveal(store: Store<AppState>, data: GraphData): () => void {
  const external = new Set(data.authors.filter((a) => a.is_itmo === false).map((a) => a.key));
  let prev = store.get();
  return store.subscribe((state) => {
    const navigated = state.selection !== prev.selection || state.tab !== prev.tab || state.screen !== prev.screen;
    prev = state;
    const { selection, filters } = state;
    if (!navigated || selection?.kind !== "node") return;
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
