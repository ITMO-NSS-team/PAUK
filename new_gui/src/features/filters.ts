// Слой "features" — регуляторы порогов фильтрации (store.filters).
// Какие регуляторы показывать, зависит от активной вкладки: "Авторы" —
// порог соавторства, "Публикации" — порог общих авторов и год, у
// "Репозиториев"/"Поиска" регуляторов нет вообще (как и в старом GUI —
// у репозиториев порога веса не было совсем).

import { FILTER_CONFIG } from "../core/config";
import { requireElement } from "../core/dom";
import { t, type Lang } from "../core/i18n";
import type { AppState, Store } from "../core/state";

interface FilterRowOptions {
  label: string;
  min: number;
  max: number;
  value: number;
  onChange: (value: number) => void;
}

/** Одна строка "подпись + ползунок + текущее значение" — тот же принцип, что и core/render.ts::renderListItem: один способ собрать строку регулятора вместо копирования разметки под каждый фильтр. */
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
  input.value = String(options.value);

  const value = document.createElement("span");
  value.className = "filter-row__value";
  value.textContent = String(options.value);

  input.addEventListener("input", () => {
    value.textContent = input.value;
    options.onChange(Number(input.value));
  });

  row.append(label, input, value);
  return row;
}

/**
 * Подключает регуляторы фильтров. Перестраивает разметку только при смене
 * вкладки или языка (state.tab/state.lang) — сам ползунок уже обновляет
 * свою подпись значения по месту через onChange, поэтому реагировать на
 * каждое изменение store целиком (в том числе на смену selection от
 * клика по карте) незачем — как и в features/tabs/index.ts::activateTab().
 */
export function mountFilters(store: Store<AppState>): () => void {
  const container = requireElement("filter-bar");

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
    const rows: HTMLElement[] = [];

    if (state.tab === 1) {
      rows.push(
        buildFilterRow({
          label: t("filter.coauth", lang),
          min: FILTER_CONFIG.coauth.min,
          max: FILTER_CONFIG.coauth.max,
          value: filters.minCoauth,
          onChange: (value) => setFilter({ minCoauth: value }),
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
      );
    }

    container.hidden = rows.length === 0;
    container.replaceChildren(...rows);
  }

  render(store.get());
  return store.subscribe(render);
}
