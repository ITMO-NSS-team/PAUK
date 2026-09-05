// Слой "map" — превращает GraphData в GeoJSON и рисует его на MapLibre.
// Логика интерпретации данных (какая подпись у узла, как искать узел по
// ключу) живёт в core/data.ts — этот файл только про геометрию и слои
// карты, ничего не решает про сами данные.
//
// Важно: карта показывает не всё сразу, а один из ТРЁХ РАЗНЫХ ГРАФОВ —
// какой набор узлов/рёбер рисовать, зависит от активной вкладки, ровно
// как tabNodes()/tabEdges() в старом core.js: вкладка "Авторы" — только
// авторы и соавторство, "Репозитории" — только репозитории и их связи,
// "Публикации" — только публикации. Показывать все сущности одновременно
// было бы другим (и неверным) поведением, а не тем же графом "покрасивее".

import type { Feature, FeatureCollection, LineString, Point } from "geojson";
import type { ExpressionSpecification, GeoJSONSource, Map as MapLibreMap } from "maplibre-gl";
import type { AuthorNode, Edge, GraphData, PubNode, RepoNode } from "../contracts/graph";
import type { SearchDetail } from "../contracts/search";
import { MAP_CONFIG } from "../core/config";
import { nodeLabel } from "../core/data";
import type { Lang } from "../core/i18n";
import type { AppState, Store, TabId } from "../core/state";

type GraphNode = AuthorNode | RepoNode | PubNode;
type Filters = AppState["filters"];
type SearchDetailsByKey = Map<string, SearchDetail>;

/** Свойства, которые кладутся в каждую GeoJSON-точку узла — доступны в paint-выражениях слоя через `["get", "имя"]`. */
interface NodeProps {
  key: string;
  kind: GraphNode["kind"];
  label: string;
  color: string;
}

/** Свойства ребра: `s`/`t` — ключи узлов на концах (нужны, чтобы построить Selection по клику), `w` — вес. */
interface EdgeProps {
  s: string;
  t: string;
  w: number;
}

/**
 * Все три вида узлов сразу, одним плоским списком. Нужна только для
 * {@link nodeBounds} (общая рамка камеры, которая должна охватывать вообще
 * всё) и для поиска позиций концов ребра в {@link buildEdgeFeatures} — не
 * для отрисовки текущей вкладки (для этого есть {@link tabGraphNodes}).
 *
 * @param data - данные графа.
 * @returns Авторы, репозитории и публикации одним списком, в этом порядке.
 */
function allNodes(data: GraphData): GraphNode[] {
  return [...data.authors, ...data.repos, ...data.pubs];
}

/**
 * Возвращает узлы, которые должна показывать активная вкладка. Вкладка 4
 * (поиск) — пустой список, карта на ней не привязана ни к одному из трёх
 * графов.
 *
 * Единственный узловой фильтр — год публикации на вкладке 3
 * (`filters.yearMax`): публикации без известного года (`year === null`)
 * никогда не скрываются этим фильтром — мы не знаем их год, а не знаем,
 * что он "слишком поздний", это разные вещи.
 *
 * @param data - данные графа.
 * @param tab - активная вкладка.
 * @param filters - текущие пороги фильтров (используется только `yearMax`, только для `tab === 3`).
 * @returns Список узлов, которые нужно нарисовать на карте для этой вкладки.
 *
 * @example
 * tabGraphNodes(data, 1, filters); // data.authors — вкладка "Авторы"
 * tabGraphNodes(data, 4, filters); // [] — у вкладки "Поиск" своего графа нет
 */
function tabGraphNodes(data: GraphData, tab: TabId, filters: Filters): GraphNode[] {
  switch (tab) {
    case 1:
      return data.authors;
    case 2:
      return data.repos;
    case 3:
      return data.pubs.filter((pub) => pub.year === null || pub.year <= filters.yearMax);
    default:
      return [];
  }
}

/**
 * Возвращает рёбра, которые должна показывать активная вкладка — тот же
 * принцип, что и {@link tabGraphNodes}, плюс порог веса: на вкладке
 * "Авторы" прячутся слабые связи соавторства (меньше `filters.minCoauth`
 * совместных публикаций), на "Публикациях" — связи между публикациями с
 * малым числом общих авторов (`filters.minSharedAuthors`). У репозиториев
 * (вкладка 2) порога веса нет вообще — как и в старом GUI.
 *
 * @param data - данные графа.
 * @param tab - активная вкладка.
 * @param filters - текущие пороги фильтров.
 * @returns Список рёбер, которые нужно нарисовать на карте для этой вкладки.
 *
 * @example
 * tabGraphEdges(data, 1, { minCoauth: 2, ... }); // только coauth_edges с w >= 2
 * tabGraphEdges(data, 2, filters);               // все repo_edges, без порога веса
 */
function tabGraphEdges(data: GraphData, tab: TabId, filters: Filters): Edge[] {
  switch (tab) {
    case 1:
      return data.coauth_edges.filter((edge) => edge.w >= filters.minCoauth);
    case 2:
      return data.repo_edges;
    case 3:
      return data.pub_edges.filter((edge) => edge.w >= filters.minSharedAuthors);
    default:
      return [];
  }
}

/**
 * Строит GeoJSON-коллекцию точек узлов активной вкладки, готовую для
 * `map.addSource`/`GeoJSONSource.setData`. Цвет узла — цвет его
 * департамента (пока без приглушения/hover-состояний — это отдельный
 * будущий шаг, только базовая раскраска, чтобы точки были различимы по
 * департаментам).
 *
 * @param data - данные графа.
 * @param lang - язык интерфейса (влияет на `properties.label`).
 * @param tab - активная вкладка.
 * @param filters - текущие пороги фильтров.
 * @param searchDetails - карта деталей публикаций (для настоящих названий публикаций в подписях).
 * @returns GeoJSON `FeatureCollection` точек с свойствами {@link NodeProps} на каждой.
 */
export function buildNodeFeatures(
  data: GraphData,
  lang: Lang,
  tab: TabId,
  filters: Filters,
  searchDetails: SearchDetailsByKey,
): FeatureCollection<Point, NodeProps> {
  // Map по id департамента, а не поиск в массиве на каждый узел —
  // департаментов немного, но узлов может быть тысячи.
  const deptById = new Map(data.departments.map((dept) => [dept.id, dept]));

  const features: Feature<Point, NodeProps>[] = tabGraphNodes(data, tab, filters).map((node) => ({
    type: "Feature",
    // id нужен, чтобы MapLibre мог адресовать конкретную фичу (например,
    // для будущей подсветки через feature-state), а не только через
    // properties — сейчас используется только properties.key, но id
    // задаём сразу, чтобы не переделывать источник данных позже.
    id: node.key,
    geometry: { type: "Point", coordinates: [node.gx, node.gy] },
    properties: {
      key: node.key,
      kind: node.kind,
      label: nodeLabel(node, lang, searchDetails),
      color: deptById.get(node.dept)?.color ?? MAP_CONFIG.node.fallbackColor,
    },
  }));

  return { type: "FeatureCollection", features };
}

/**
 * Строит GeoJSON-коллекцию линий рёбер активной вкладки — только тех, для
 * которых нашлись обе позиции. Позиции ищутся среди ВСЕХ узлов
 * ({@link allNodes}), а не только узлов текущей вкладки: концы ребра всегда
 * того же вида, что и сама вкладка (например, `coauth_edges` всегда между
 * авторами), так что это не смешивает графы, а просто самый простой способ
 * получить карту "ключ -> координаты".
 *
 * Ребро между публикациями, у одной из которых год скрыт фильтром года
 * (см. {@link tabGraphNodes}), автоматически пропадает не через "нет
 * позиции" (позиция физически есть в `allNodes`), а через отдельную
 * проверку `pubYearByKey` ниже — "нет позиции" здесь означает буквально
 * "нет такого ключа в authors/repos/pubs", а не "скрыт фильтром".
 *
 * @param data - данные графа.
 * @param tab - активная вкладка.
 * @param filters - текущие пороги фильтров.
 * @returns GeoJSON `FeatureCollection` линий с свойствами {@link EdgeProps} на каждой.
 */
export function buildEdgeFeatures(data: GraphData, tab: TabId, filters: Filters): FeatureCollection<LineString, EdgeProps> {
  const posByKey = new Map(allNodes(data).map((node) => [node.key, [node.gx, node.gy] as [number, number]]));
  const pubYearByKey = new Map(data.pubs.map((pub) => [pub.key, pub.year]));

  const features: Feature<LineString, EdgeProps>[] = [];
  for (const edge of tabGraphEdges(data, tab, filters)) {
    const s = posByKey.get(edge.s);
    const t = posByKey.get(edge.t);
    // Ребро без одной из позиций значит, что второй конец не попал в
    // authors/repos/pubs (например, отфильтрован раньше) — такое ребро
    // рисовать некуда, молча пропускаем, а не падаем с ошибкой.
    if (!s || !t) continue;

    if (tab === 3) {
      const sYear = pubYearByKey.get(edge.s);
      const tYear = pubYearByKey.get(edge.t);
      // Публикация без известного года (null/undefined) фильтром по году
      // не задевается — прячем ребро, только если год ИЗВЕСТЕН и превышает порог.
      if ((sYear ?? filters.yearMax) > filters.yearMax || (tYear ?? filters.yearMax) > filters.yearMax) continue;
    }

    features.push({
      type: "Feature",
      geometry: { type: "LineString", coordinates: [s, t] },
      properties: { s: edge.s, t: edge.t, w: edge.w },
    });
  }

  return { type: "FeatureCollection", features };
}

/**
 * Вычисляет прямоугольник, охватывающий вообще все узлы (а не только
 * текущей вкладки) — камера подгоняется под него один раз при загрузке и
 * больше не трогается при переключении вкладок (так же вело себя старое
 * `main.js`: `fitBounds` там был на фиксированный box, общий для всех вкладок).
 *
 * @param data - данные графа.
 * @returns Пара координат `[[minLon, minLat], [maxLon, maxLat]]`, формат,
 *   который MapLibre принимает напрямую в `map.fitBounds(...)`.
 */
export function nodeBounds(data: GraphData): [[number, number], [number, number]] {
  const nodes = allNodes(data);
  const lons = nodes.map((node) => node.gx);
  const lats = nodes.map((node) => node.gy);
  return [
    [Math.min(...lons), Math.min(...lats)],
    [Math.max(...lons), Math.max(...lats)],
  ];
}

// Экспортируем id-константы наружу (не просто private) — features/selection.ts
// должен знать, по какому слою кликать и на каком слое менять paint-свойства
// подсветки, чтобы не дублировать строки-литералы в двух файлах.
/** id слоя с видимыми точками узлов (MapLibre `circle`-слой). */
export const NODE_LAYER_ID = "graph-nodes-circle";
/** id слоя с видимыми линиями рёбер (MapLibre `line`-слой, тонкий). */
export const EDGE_LAYER_ID = "graph-edges-line";
// Невидимый слой поверх того же источника — шире видимой линии, нужен
// только для клика/наведения (features/selection.ts). Разделять "как
// выглядит" и "где кликается" — стандартный приём для тонких линий:
// сделать линию визуально тонкой, но реальную область попадания шире.
/** id невидимого слоя-приёмника кликов по рёбрам (шире видимой линии, `line-opacity: 0`). */
export const EDGE_HIT_LAYER_ID = "graph-edges-hit";
const NODE_SOURCE_ID = "graph-nodes";
const EDGE_SOURCE_ID = "graph-edges";

// Ключ узла в GeoJSON-свойствах никогда не бывает пустой строкой (это
// либо OpenAlex-подобный id автора/публикации, либо repo-ключ) — поэтому
// пустая строка безопасно используется как "ничего не выбрано": ни одна
// настоящая точка на карте с ней не совпадёт.
const NO_SELECTION = "";

/**
 * Строит MapLibre-выражение вида "если это выбранная точка (по
 * `properties.key`) — `selectedValue`, иначе — `defaultValue`". Нужно и при
 * первой отрисовке (в {@link addGraphLayers}), и при каждой смене выбора
 * (в {@link setSelectedNode}) — раньше оба места писали это выражение и
 * оба числа (`8`/`4`, `2`/`1`) заново, что уже приводило к рассинхрону при
 * правке. Теперь оба вызывающих места используют одну функцию и одни
 * значения из {@link MAP_CONFIG}.
 *
 * @param compareTo - ключ узла, который считается выбранным (или {@link NO_SELECTION}, если не выбрано ничего).
 * @param selectedValue - значение paint-свойства для выбранного узла.
 * @param defaultValue - значение paint-свойства для всех остальных узлов.
 * @returns MapLibre-выражение, пригодное для paint-свойства слоя.
 */
function selectedNodeExpression(
  compareTo: string,
  selectedValue: number,
  defaultValue: number,
): ExpressionSpecification {
  return ["case", ["==", ["get", "key"], compareTo], selectedValue, defaultValue];
}

/**
 * То же самое, что и {@link selectedNodeExpression}, но для ребра — у него
 * нет одного ключа, есть пара `(s, t)` на концах, поэтому сравниваются оба
 * сразу через `["all", ...]`.
 *
 * @param compareS - `s` выбранного ребра (или {@link NO_SELECTION}).
 * @param compareT - `t` выбранного ребра (или {@link NO_SELECTION}).
 * @param selectedValue - значение paint-свойства для выбранного ребра.
 * @param defaultValue - значение paint-свойства для всех остальных рёбер.
 * @returns MapLibre-выражение, пригодное для paint-свойства слоя.
 */
function selectedEdgeExpression(
  compareS: string,
  compareT: string,
  selectedValue: number,
  defaultValue: number,
): ExpressionSpecification {
  return [
    "case",
    ["all", ["==", ["get", "s"], compareS], ["==", ["get", "t"], compareT]],
    selectedValue,
    defaultValue,
  ];
}

/**
 * Добавляет источники и слои узлов/рёбер текущей вкладки на карту.
 * Вызывается один раз изнутри {@link mountReactiveGraph}, при первом
 * монтировании графа — дальнейшие обновления идут через
 * {@link refreshGraphLayers} (источники уже существуют, меняются только данные).
 *
 * @param map - экземпляр карты MapLibre.
 * @param data - данные графа.
 * @param lang - язык интерфейса.
 * @param tab - активная вкладка на момент монтирования.
 * @param filters - текущие пороги фильтров.
 * @param searchDetails - карта деталей публикаций.
 */
function addGraphLayers(
  map: MapLibreMap,
  data: GraphData,
  lang: Lang,
  tab: TabId,
  filters: Filters,
  searchDetails: SearchDetailsByKey,
): void {
  map.addSource(EDGE_SOURCE_ID, { type: "geojson", data: buildEdgeFeatures(data, tab, filters) });
  map.addLayer({
    id: EDGE_LAYER_ID,
    type: "line",
    source: EDGE_SOURCE_ID,
    paint: {
      "line-color": MAP_CONFIG.edge.color,
      // Тот же приём, что и у circle-radius/circle-stroke-width узла ниже:
      // одно выражение и для первой отрисовки, и для setSelectedEdge() —
      // изначально не выбрано ничего.
      "line-width": selectedEdgeExpression(NO_SELECTION, NO_SELECTION, MAP_CONFIG.edge.widthSelected, MAP_CONFIG.edge.width),
      "line-opacity": selectedEdgeExpression(
        NO_SELECTION,
        NO_SELECTION,
        MAP_CONFIG.edge.opacitySelected,
        MAP_CONFIG.edge.opacity,
      ),
    },
  });
  // Тот же источник, полностью прозрачная широкая линия — реальная область
  // клика/наведения (см. features/selection.ts), сама видимая линия выше
  // остаётся тонкой. Клик по узлу это не задевает: узловой слой
  // запрашивается отдельно и первым (см. mountSelection), этот слой шире
  // только для рёбер.
  map.addLayer({
    id: EDGE_HIT_LAYER_ID,
    type: "line",
    source: EDGE_SOURCE_ID,
    paint: { "line-width": MAP_CONFIG.edge.hitWidth, "line-opacity": 0 },
  });

  map.addSource(NODE_SOURCE_ID, { type: "geojson", data: buildNodeFeatures(data, lang, tab, filters, searchDetails) });
  map.addLayer({
    id: NODE_LAYER_ID,
    type: "circle",
    source: NODE_SOURCE_ID,
    paint: {
      // selectedNodeExpression сравнивает properties.key текущей точки с
      // выбранным ключом (setSelectedNode ниже подставляет его сюда через
      // setPaintProperty) и рисует крупнее с более толстой обводкой именно
      // выбранный узел, остальные — как обычно. Изначально не выбрано ничего.
      "circle-radius": selectedNodeExpression(NO_SELECTION, MAP_CONFIG.node.radiusSelected, MAP_CONFIG.node.radius),
      "circle-color": ["get", "color"],
      "circle-stroke-width": selectedNodeExpression(
        NO_SELECTION,
        MAP_CONFIG.node.strokeWidthSelected,
        MAP_CONFIG.node.strokeWidth,
      ),
      "circle-stroke-color": MAP_CONFIG.node.strokeColor,
    },
  });
}

/**
 * Пересобирает узлы и рёбра под новые вкладку/язык/фильтры — источники уже
 * добавлены ({@link addGraphLayers} к этому моменту уже вызывалась один
 * раз), здесь только `setData()` на существующих источниках.
 *
 * @param map - экземпляр карты MapLibre.
 * @param data - данные графа.
 * @param lang - новый язык интерфейса.
 * @param tab - новая активная вкладка.
 * @param filters - новые пороги фильтров.
 * @param searchDetails - карта деталей публикаций.
 */
function refreshGraphLayers(
  map: MapLibreMap,
  data: GraphData,
  lang: Lang,
  tab: TabId,
  filters: Filters,
  searchDetails: SearchDetailsByKey,
): void {
  const nodeSource = map.getSource(NODE_SOURCE_ID) as GeoJSONSource | undefined;
  nodeSource?.setData(buildNodeFeatures(data, lang, tab, filters, searchDetails));

  const edgeSource = map.getSource(EDGE_SOURCE_ID) as GeoJSONSource | undefined;
  edgeSource?.setData(buildEdgeFeatures(data, tab, filters));
}

/**
 * Подсвечивает выбранный узел на карте (крупнее, с более толстой обводкой)
 * и снимает подсветку с остальных. `key === null` — снять выделение
 * совсем. Категориальный цвет узла (`circle-color`, цвет департамента) при
 * этом не трогается — выделение показывается размером и обводкой, а не
 * сменой цвета.
 *
 * @param map - экземпляр карты MapLibre.
 * @param key - ключ узла, который нужно подсветить, либо `null`, чтобы снять подсветку.
 *
 * @example
 * setSelectedNode(map, "A1"); // A1 рисуется крупнее остальных
 * setSelectedNode(map, null); // подсветка снята, все узлы обычного размера
 */
export function setSelectedNode(map: MapLibreMap, key: string | null): void {
  const compareTo = key ?? NO_SELECTION;
  map.setPaintProperty(
    NODE_LAYER_ID,
    "circle-radius",
    selectedNodeExpression(compareTo, MAP_CONFIG.node.radiusSelected, MAP_CONFIG.node.radius),
  );
  map.setPaintProperty(
    NODE_LAYER_ID,
    "circle-stroke-width",
    selectedNodeExpression(compareTo, MAP_CONFIG.node.strokeWidthSelected, MAP_CONFIG.node.strokeWidth),
  );
}

/**
 * То же самое, что и {@link setSelectedNode}, но для ребра — вместо
 * "крупнее" выбранное ребро рисуется толще и полностью непрозрачным
 * (обычные рёбра — тонкие и полупрозрачные, см. {@link MAP_CONFIG}).
 * `edge === null` снимает выделение совсем, ровно как `key === null` у
 * {@link setSelectedNode}.
 *
 * @param map - экземпляр карты MapLibre.
 * @param edge - концы ребра, которое нужно подсветить (`s`/`t`, порядок как в данных), либо `null`.
 *
 * @example
 * setSelectedEdge(map, { s: "A1", t: "A2" }); // это ребро толще и непрозрачнее остальных
 * setSelectedEdge(map, null); // подсветка снята
 */
export function setSelectedEdge(map: MapLibreMap, edge: { s: string; t: string } | null): void {
  const compareS = edge?.s ?? NO_SELECTION;
  const compareT = edge?.t ?? NO_SELECTION;
  map.setPaintProperty(
    EDGE_LAYER_ID,
    "line-width",
    selectedEdgeExpression(compareS, compareT, MAP_CONFIG.edge.widthSelected, MAP_CONFIG.edge.width),
  );
  map.setPaintProperty(
    EDGE_LAYER_ID,
    "line-opacity",
    selectedEdgeExpression(compareS, compareT, MAP_CONFIG.edge.opacitySelected, MAP_CONFIG.edge.opacity),
  );
}

/**
 * Единственная точка входа для отрисовки графа: добавляет слои под текущее
 * состояние (`store.get()`) и дальше сама следит за store — пересобирает
 * узлы/рёбра при смене вкладки, языка ИЛИ фильтров. Раньше три разных
 * места (`app/main.ts`, `features/tabs/index.ts`, `features/langToggle.ts`)
 * сами решали, когда дёргать пересборку графа, — теперь это знает только
 * сам граф, а остальным фичам достаточно менять `store.tab`/`lang`/`filters`,
 * не заботясь о том, что ещё нужно перерисовать.
 *
 * Смена `selection` намеренно НЕ пересобирает граф (подсветка выбранного
 * узла/ребра — отдельная, гораздо более дешёвая операция через
 * {@link setSelectedNode}/{@link setSelectedEdge} в features/selection.ts,
 * без пересоздания всего источника данных).
 *
 * @param map - экземпляр карты MapLibre.
 * @param store - Store приложения.
 * @param data - данные графа.
 * @param searchDetails - карта деталей публикаций.
 * @returns Функция отписки от store (unmount) — снимает подписку, добавленную этим вызовом.
 */
export function mountReactiveGraph(
  map: MapLibreMap,
  store: Store<AppState>,
  data: GraphData,
  searchDetails: SearchDetailsByKey,
): () => void {
  const initial = store.get();
  addGraphLayers(map, data, initial.lang, initial.tab, initial.filters, searchDetails);

  let prev = initial;
  const unsubscribe = store.subscribe((state) => {
    // filters — новый объект только когда его реально меняли (Store.set
    // мержит патч поверх состояния, не трогая поля вне патча), поэтому
    // сравнение по ссылке здесь корректно и дешевле глубокого сравнения.
    if (state.tab === prev.tab && state.lang === prev.lang && state.filters === prev.filters) return;
    prev = state;
    refreshGraphLayers(map, data, state.lang, state.tab, state.filters, searchDetails);
  });

  return unsubscribe;
}
