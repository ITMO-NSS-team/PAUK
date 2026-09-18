// Слой "features" — регуляторы порогов фильтрации (store.filters).
// Какие регуляторы показывать, зависит от активной вкладки: "Авторы" —
// порог соавторства, "Публикации" — порог общих авторов и год, у
// "Репозиториев"/"Поиска" регуляторов нет вообще (как и в старом GUI —
// у репозиториев порога веса не было совсем).

import type { GraphData } from "../contracts/graph";
import { FILTER_CONFIG } from "../core/config";
import { requireElement } from "../core/dom";
import { t, type Lang } from "../core/i18n";
import type { AppState, Store } from "../core/state";

/** Параметры одной строки регулятора — вход {@link buildFilterRow}. */
interface FilterRowOptions {
  /** Текст подписи слева от ползунка. */
  label: string;
  /** Минимально допустимое значение ползунка. */
  min: number;
  /** Максимально допустимое значение ползунка. */
  max: number;
  /** Шаг ползунка — по умолчанию 1 (счётные пороги вроде "число публикаций"); дробный для непрерывных величин вроде camera.ratio. */
  step?: number;
  /** Текущее значение ползунка. */
  value: number;
  /**
   * Вызывается с новым числовым значением — не на каждый тик перетаскивания,
   * а с задержкой {@link FILTER_CONFIG.debounceMs} после того, как
   * пользователь остановился (см. {@link buildFilterRow}): применение
   * фильтра пересобирает граф, дёргать это на каждый пиксель перетаскивания
   * ощущалось как лаг.
   */
  onChange: (value: number) => void;
}

/**
 * Собирает одну строку "подпись + ползунок + текущее значение" — тот же
 * принцип, что и `core/render.ts::renderListItem`: один способ собрать
 * строку регулятора вместо копирования разметки под каждый фильтр.
 *
 * @param options - см. {@link FilterRowOptions}.
 * @returns Готовый `<label class="filter-row">` с ползунком внутри, ещё не вставленный в DOM.
 */
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

  // Подпись значения — сразу, на каждый тик (это просто DOM-текст, не
  // тормозит). options.onChange — с задержкой (debounce): применение
  // фильтра пересобирает весь граф (map/build.ts::populateGraph), на
  // реальных данных это заметно тяжелее одного движения ползунка — без
  // задержки перетаскивание гоняло полную пересборку на каждый пиксель и
  // лагало (прямая жалоба). Таймер один на строку (в замыкании) — новое
  // движение сбрасывает предыдущий отсчёт, применяется только последнее
  // значение, на котором пользователь реально остановился.
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

/** Параметры одной строки чекбокса — вход {@link buildCheckboxRow}. */
interface CheckboxRowOptions {
  /** Текст подписи рядом с чекбоксом. */
  label: string;
  /** Текущее состояние чекбокса. */
  checked: boolean;
  /** Вызывается при каждом клике по чекбоксу с новым состоянием. */
  onChange: (checked: boolean) => void;
}

/**
 * Собирает одну строку "чекбокс + подпись" — тот же принцип, что и
 * {@link buildFilterRow}, но для булевых фильтров (например, "показывать
 * без департамента"), а не числовых порогов.
 *
 * @param options - см. {@link CheckboxRowOptions}.
 * @returns Готовый `<label class="filter-row">` с чекбоксом внутри, ещё не вставленный в DOM.
 */
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
 * Подключает регуляторы фильтров для активной вкладки. Перестраивает
 * разметку только при смене вкладки или языка (`state.tab`/`state.lang`) —
 * сам ползунок уже обновляет свою подпись значения по месту через
 * `onChange`, поэтому реагировать на каждое изменение store целиком (в том
 * числе на смену `selection` от клика по карте) незачем — как и в
 * `features/tabs/index.ts::activateTab()`.
 *
 * @param store - Store приложения.
 * @returns Функция отписки (unmount) от Store.
 */
export function mountFilters(store: Store<AppState>): () => void {
  const container = requireElement("filter-bar");
  const sectionLabel = requireElement("filters-section-label");

  let prevTab: AppState["tab"] | null = null;
  let prevLang: Lang | null = null;

  /**
   * Точечно обновляет пороги фильтров в Store, мержа `patch` поверх
   * текущих `filters` (по тому же принципу, что и сам `Store.set`).
   *
   * @param patch - изменяемые поля фильтров (обычно одно поле за раз, из `onChange` конкретного ползунка).
   */
  function setFilter(patch: Partial<AppState["filters"]>): void {
    store.set({ filters: { ...store.get().filters, ...patch } });
  }

  /**
   * Перестраивает разметку регуляторов под текущую вкладку/язык. Не
   * делает ничего, если ни то, ни другое не изменилось с прошлого вызова
   * (см. `prevTab`/`prevLang` выше) — иначе разметка пересобиралась бы на
   * любое изменение store, включая смену `selection`.
   *
   * @param state - текущее состояние приложения.
   */
  function render(state: AppState): void {
    if (state.tab === prevTab && state.lang === prevLang) return;
    prevTab = state.tab;
    prevLang = state.lang;

    const { lang, filters } = state;
    sectionLabel.textContent = t("section.filters", lang);
    // Порог видимости рёбер по зуму — общий для всех трёх вкладок (это
    // настройка отрисовки карты, а не фильтр конкретного вида сущностей),
    // поэтому строится один раз, а не внутри if/else по вкладке ниже.
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

    // Регионы департаментов (map/regions.ts) — после фильтров вкладки:
    // переключатель свой у каждой вкладки, пороги общие.
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

    // Раньше скрывался, если для вкладки не было ни одного регулятора
    // (у "Репозиториев" не было своих) — с общим для всех вкладок
    // регулятором зума рёбер выше строк всегда хотя бы одна, панель всегда видна.
    container.hidden = false;
    container.replaceChildren(...rows);
  }

  render(store.get());
  return store.subscribe(render);
}

/**
 * Включает `filters.showExternalAuthors`, как только выбран внешний автор
 * (ссылка из карточки публикации, URL): иначе выбор ушёл бы в узел,
 * которого нет на карте. Подписываться нужно раньше
 * `map/build.ts::mountReactiveGraph` — тот сбрасывает выбор узла, которого
 * нет в графе.
 *
 * @param store - общий Store приложения.
 * @param data - данные графа (какие авторы внешние).
 * @returns Функция отписки.
 */
export function mountExternalAuthorReveal(store: Store<AppState>, data: GraphData): () => void {
  const external = new Set(data.authors.filter((a) => a.is_itmo === false).map((a) => a.key));
  return store.subscribe((state) => {
    const { selection, filters } = state;
    if (!filters.showExternalAuthors && selection?.kind === "node" && external.has(selection.key)) {
      store.set({ filters: { ...filters, showExternalAuthors: true } });
    }
  });
}
