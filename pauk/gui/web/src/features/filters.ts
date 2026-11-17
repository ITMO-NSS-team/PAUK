import type { GraphData } from "../contracts/graph";
import { FILTER_CONFIG } from "../core/config";
import { isolatedAuthors, noDeptId } from "../map/build";
import { requireElement } from "../core/dom";
import { t, type Lang } from "../core/i18n";
import type { AppState, Store } from "../core/state";

interface FilterRowOptions {
  key: keyof AppState["filters"];
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
  input.dataset.filter = options.key;
  input.dataset.syncedValue = String(options.value);
  input.min = String(options.min);
  input.max = String(Math.max(options.max, options.value));
  input.step = String(options.step ?? 1);
  input.value = String(options.value);

  const value = document.createElement("span");
  value.className = "filter-row__value";
  value.textContent = String(options.value);

  // The value label updates on every tick, onChange is debounced: a graph
  // rebuild per pixel of dragging lags.
  let debounceTimer: ReturnType<typeof setTimeout> | undefined;
  // The thumb tilts toward the drag direction, see index.html.
  let lastValue = options.value;
  input.addEventListener("filter-sync", () => {
    clearTimeout(debounceTimer);
    lastValue = Number(input.value);
    value.textContent = input.value;
  });
  input.addEventListener("pointerdown", () => delete input.dataset.tilt);
  input.addEventListener("input", () => {
    value.textContent = input.value;
    const parsed = Number(input.value);
    if (parsed !== lastValue) input.dataset.tilt = parsed < lastValue ? "left" : "right";
    lastValue = parsed;
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => options.onChange(parsed), FILTER_CONFIG.debounceMs);
  });

  row.append(label, input, value);
  return row;
}

interface CheckboxRowOptions {
  key: keyof AppState["filters"];
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}

function buildCheckboxRow(options: CheckboxRowOptions): HTMLElement {
  const row = document.createElement("label");
  row.className = "filter-row";

  const input = document.createElement("input");
  input.type = "checkbox";
  input.dataset.filter = options.key;
  input.dataset.syncedValue = String(options.checked);
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
    if (state.tab === prevTab && state.lang === prevLang) {
      for (const input of container.querySelectorAll<HTMLInputElement>("input[data-filter]")) {
        const key = input.dataset.filter as keyof AppState["filters"];
        const next =
          key === "showRegions" ? state.filters.showRegions[state.tab] : state.filters[key];
        if (input.dataset.syncedValue === String(next)) continue;
        input.dataset.syncedValue = String(next);
        if (input.type === "checkbox") input.checked = Boolean(next);
        else {
          input.max = String(Math.max(Number(input.max), Number(next)));
          input.value = String(next);
          input.dispatchEvent(new Event("filter-sync"));
        }
      }
      return;
    }
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
        key: "edgeZoomThreshold",
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
          key: "minCoauth",
          value: filters.minCoauth,
          onChange: (value) => setFilter({ minCoauth: value }),
        }),
        buildCheckboxRow({
          label: t("filter.showNoDept", lang),
          key: "showNoDeptAuthors",
          checked: filters.showNoDeptAuthors,
          onChange: (checked) => setFilter({ showNoDeptAuthors: checked }),
        }),
        buildCheckboxRow({
          label: t("filter.showExternal", lang),
          key: "showExternalAuthors",
          checked: filters.showExternalAuthors,
          onChange: (checked) => setFilter({ showExternalAuthors: checked }),
        }),
        buildCheckboxRow({
          label: t("filter.showIsolated", lang),
          key: "showIsolatedAuthors",
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
          key: "minSharedAuthors",
          value: filters.minSharedAuthors,
          onChange: (value) => setFilter({ minSharedAuthors: value }),
        }),
        buildFilterRow({
          label: t("filter.yearMax", lang),
          min: FILTER_CONFIG.year.min,
          max: FILTER_CONFIG.year.max,
          key: "yearMax",
          value: filters.yearMax,
          onChange: (value) => setFilter({ yearMax: value }),
        }),
        buildCheckboxRow({
          label: t("filter.showNoDept", lang),
          key: "showNoDeptPubs",
          checked: filters.showNoDeptPubs,
          onChange: (checked) => setFilter({ showNoDeptPubs: checked }),
        }),
      );
    }

    // Region toggle is per tab, region thresholds are shared.
    rows.push(
      buildCheckboxRow({
        label: t("filter.showRegions", lang),
        key: "showRegions",
        checked: filters.showRegions[state.tab],
        onChange: (checked) =>
          setFilter({ showRegions: { ...store.get().filters.showRegions, [state.tab]: checked } }),
      }),
      buildFilterRow({
        label: t("filter.regionZoom", lang),
        min: FILTER_CONFIG.regionZoom.min,
        max: FILTER_CONFIG.regionZoom.max,
        step: FILTER_CONFIG.regionZoom.step,
        key: "regionZoomThreshold",
        value: filters.regionZoomThreshold,
        onChange: (value) => setFilter({ regionZoomThreshold: value }),
      }),
      buildFilterRow({
        label: t("filter.regionMinNodes", lang),
        min: FILTER_CONFIG.regionMinNodes.min,
        max: FILTER_CONFIG.regionMinNodes.max,
        key: "regionMinNodes",
        value: filters.regionMinNodes,
        onChange: (value) => setFilter({ regionMinNodes: value }),
      }),
    );

    container.hidden = false;
    const advanced = document.createElement("details");
    advanced.className = "filter-settings";
    const summary = document.createElement("summary");
    summary.textContent = t("filter.displayOptions", lang);
    advanced.append(summary);
    const basic = rows.filter((row) => {
      const key = row.querySelector<HTMLInputElement>("input")?.dataset.filter;
      if (
        key === "edgeZoomThreshold" ||
        key === "regionZoomThreshold" ||
        key === "regionMinNodes"
      ) {
        advanced.append(row);
        return false;
      }
      return true;
    });
    container.replaceChildren(...basic, advanced);
  }

  render(store.get());
  return store.subscribe(render);
}

/**
 * Reveals a selected author/publication hidden by node filters,
 * e.g. after a global search or a link from another card. Must subscribe before
 * map/build.ts::mountReactiveGraph, which drops selections missing from the graph.
 *
 * @returns Unmount function.
 */
export function mountHiddenAuthorReveal(store: Store<AppState>, data: GraphData): () => void {
  const authors = new Map(data.authors.map((node) => [node.key, node]));
  const pubs = new Map(data.pubs.map((node) => [node.key, node]));
  const excludedDept = noDeptId(data);
  let previousSelection = store.get().selection;
  return store.subscribe((state) => {
    const { selection, filters } = state;
    if (selection === previousSelection) return;
    previousSelection = selection;
    const notice = document.getElementById("filter-notice");
    if (notice) notice.hidden = true;
    if (selection?.kind !== "node") return;
    const author = authors.get(selection.key);
    const pub = pubs.get(selection.key);
    const patch: Partial<AppState["filters"]> = {};
    if (author) {
      if (author.is_itmo === false && !filters.showExternalAuthors)
        patch.showExternalAuthors = true;
      if (
        !filters.showIsolatedAuthors &&
        isolatedAuthors(data, filters.showExternalAuthors || !!patch.showExternalAuthors).has(
          author.key,
        )
      )
        patch.showIsolatedAuthors = true;
      if (author.dept === excludedDept && !filters.showNoDeptAuthors)
        patch.showNoDeptAuthors = true;
    }
    if (pub) {
      if (pub.year !== null && pub.year > filters.yearMax) patch.yearMax = pub.year;
      if (pub.dept === excludedDept && !filters.showNoDeptPubs) patch.showNoDeptPubs = true;
    }
    if (Object.keys(patch).length > 0) {
      store.set({ filters: { ...filters, ...patch } });
      if (notice) {
        const changed = [];
        if (patch.showExternalAuthors) changed.push(t("filter.showExternal", state.lang));
        if (patch.showIsolatedAuthors) changed.push(t("filter.showIsolated", state.lang));
        if (patch.showNoDeptAuthors || patch.showNoDeptPubs)
          changed.push(t("filter.showNoDept", state.lang));
        if (patch.yearMax) changed.push(`${t("filter.yearMax", state.lang)}: ${patch.yearMax}`);
        notice.textContent = `${t("filter.revealed", state.lang)} ${changed.join(", ")}`;
        notice.hidden = false;
      }
    }
  });
}
