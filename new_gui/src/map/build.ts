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
import { MAP_CONFIG } from "../core/config";
import { nodeLabel } from "../core/data";
import type { Lang } from "../core/i18n";
import type { AppState, Store, TabId } from "../core/state";

type GraphNode = AuthorNode | RepoNode | PubNode;
type Filters = AppState["filters"];

/** Свойства, которые кладутся в каждую GeoJSON-точку узла — доступны в paint-выражениях слоя через ["get", "имя"]. */
interface NodeProps {
  key: string;
  kind: GraphNode["kind"];
  label: string;
  color: string;
}

/** Свойства ребра: s/t — ключи узлов на концах (нужны, чтобы построить Selection по клику), w — вес. */
interface EdgeProps {
  s: string;
  t: string;
  w: number;
}

/** Все три вида узлов сразу — нужно только для nodeBounds() (общая рамка камеры) и для поиска позиций концов ребра, не для отрисовки. */
function allNodes(data: GraphData): GraphNode[] {
  return [...data.authors, ...data.repos, ...data.pubs];
}

/**
 * Какие узлы показывает вкладка. Вкладки 4 (поиск) — пустой список, карта
 * не привязана ни к одному из трёх графов. Единственный узловой фильтр —
 * год публикации на вкладке 3 (filters.yearMax); публикации без известного
 * года (year === null) никогда не скрываются фильтром по году — мы не
 * знаем их год, а не знаем, что он "слишком поздний".
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
 * Какие рёбра показывает вкладка — тот же принцип, что и у tabGraphNodes(),
 * плюс порог веса: на вкладке "Авторы" прячем слабое соавторство (меньше
 * filters.minCoauth совместных публикаций), на "Публикациях" — связи
 * между публикациями с малым числом общих авторов (filters.minSharedAuthors).
 * У репозиториев (вкладка 2) порога веса нет вообще — как и в старом GUI.
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
 * Цвет узла — цвет его департамента. Пока без приглушения/hover-состояний
 * (core/colors.ts с этой логикой — отдельный следующий шаг), только базовая
 * раскраска, чтобы точки на карте были различимы по департаментам.
 */
export function buildNodeFeatures(
  data: GraphData,
  lang: Lang,
  tab: TabId,
  filters: Filters,
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
      label: nodeLabel(node, lang),
      color: deptById.get(node.dept)?.color ?? MAP_CONFIG.node.fallbackColor,
    },
  }));

  return { type: "FeatureCollection", features };
}

/**
 * Рёбра текущей вкладки — только те, для которых есть обе позиции.
 * Позиции ищем среди ВСЕХ узлов (allNodes), а не только узлов текущей
 * вкладки — концы ребра всегда того же вида, что и сама вкладка (например,
 * coauth_edges всегда между авторами), так что это не смешивает графы,
 * а просто самый простой способ получить карту "ключ -> координаты".
 * Ребро между публикациями, у одной из которых год скрыт фильтром
 * (tabGraphNodes выше), автоматически пропадает тем же путём — обе
 * позиции ищутся в allNodes (там публикация всё ещё есть физически), но
 * "нет позиции" здесь означает буквально "нет такого ключа в authors/
 * repos/pubs", а не "скрыт фильтром" — поэтому год отдельно проверяется
 * ниже через posByYear.
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
 * Прямоугольник, охватывающий вообще все узлы (а не только текущей
 * вкладки) — камера подгоняется под него один раз при загрузке и больше
 * не трогается при переключении вкладок (так же вело себя старое
 * main.js: fitBounds там был на фиксированный box, общий для всех вкладок).
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
export const NODE_LAYER_ID = "graph-nodes-circle";
export const EDGE_LAYER_ID = "graph-edges-line";
// Невидимый слой поверх того же источника — шире видимой линии, нужен
// только для клика/наведения (features/selection.ts). Разделять "как
// выглядит" и "где кликается" — стандартный приём для тонких линий:
// сделать линию визуально тонкой, но реальную область попадания шире.
export const EDGE_HIT_LAYER_ID = "graph-edges-hit";
const NODE_SOURCE_ID = "graph-nodes";
const EDGE_SOURCE_ID = "graph-edges";

// Ключ узла в GeoJSON-свойствах никогда не бывает пустой строкой (это
// либо OpenAlex-подобный id автора/публикации, либо repo-ключ) — поэтому
// пустая строка безопасно используется как "ничего не выбрано": ни одна
// настоящая точка на карте с ней не совпадёт.
const NO_SELECTION = "";

/**
 * Одно и то же MapLibre-выражение "если это выбранная точка — value1,
 * иначе — value2" нужно и при первой отрисовке, и при каждой смене выбора
 * (setSelectedNode) — раньше оба места писали это выражение и оба числа
 * (8/4, 2/1) заново, что уже привело к рассинхрону при правке. Теперь оба
 * вызывающих места используют одну функцию и одни значения из MAP_CONFIG.
 */
function selectedNodeExpression(
  compareTo: string,
  selectedValue: number,
  defaultValue: number,
): ExpressionSpecification {
  return ["case", ["==", ["get", "key"], compareTo], selectedValue, defaultValue];
}

/** Добавляет источники и слои узлов/рёбер текущей вкладки — вызывается один раз изнутри mountReactiveGraph(). */
function addGraphLayers(map: MapLibreMap, data: GraphData, lang: Lang, tab: TabId, filters: Filters): void {
  map.addSource(EDGE_SOURCE_ID, { type: "geojson", data: buildEdgeFeatures(data, tab, filters) });
  map.addLayer({
    id: EDGE_LAYER_ID,
    type: "line",
    source: EDGE_SOURCE_ID,
    paint: {
      "line-color": MAP_CONFIG.edge.color,
      "line-width": MAP_CONFIG.edge.width,
      "line-opacity": MAP_CONFIG.edge.opacity,
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

  map.addSource(NODE_SOURCE_ID, { type: "geojson", data: buildNodeFeatures(data, lang, tab, filters) });
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

/** Пересобирает узлы и рёбра под новые вкладку/язык/фильтры — источники уже добавлены (addGraphLayers), здесь только setData(). */
function refreshGraphLayers(map: MapLibreMap, data: GraphData, lang: Lang, tab: TabId, filters: Filters): void {
  const nodeSource = map.getSource(NODE_SOURCE_ID) as GeoJSONSource | undefined;
  nodeSource?.setData(buildNodeFeatures(data, lang, tab, filters));

  const edgeSource = map.getSource(EDGE_SOURCE_ID) as GeoJSONSource | undefined;
  edgeSource?.setData(buildEdgeFeatures(data, tab, filters));
}

/**
 * Подсвечивает выбранный узел на карте (крупнее, с более толстой обводкой)
 * и снимает подсветку с остальных. key === null — снять выделение совсем.
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
 * Единственная точка входа для отрисовки графа: добавляет слои под текущее
 * состояние (store.get()) и дальше сама следит за store — пересобирает
 * узлы/рёбра при смене вкладки, языка ИЛИ фильтров. Раньше три разных
 * места (main.ts, features/tabs/index.ts, features/langToggle.ts) сами
 * решали, когда дёргать пересборку графа, — теперь это знает только сам
 * граф, а остальным фичам достаточно менять store.tab/lang/filters, не
 * заботясь о том, что ещё нужно перерисовать.
 */
export function mountReactiveGraph(map: MapLibreMap, store: Store<AppState>, data: GraphData): () => void {
  const initial = store.get();
  addGraphLayers(map, data, initial.lang, initial.tab, initial.filters);

  let prev = initial;
  const unsubscribe = store.subscribe((state) => {
    // filters — новый объект только когда его реально меняли (Store.set
    // мержит патч поверх состояния, не трогая поля вне патча), поэтому
    // сравнение по ссылке здесь корректно и дешевле глубокого сравнения.
    if (state.tab === prev.tab && state.lang === prev.lang && state.filters === prev.filters) return;
    prev = state;
    refreshGraphLayers(map, data, state.lang, state.tab, state.filters);
  });

  return unsubscribe;
}
