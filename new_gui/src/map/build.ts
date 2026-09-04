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
import type { TabId } from "../core/state";

type GraphNode = AuthorNode | RepoNode | PubNode;

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

/** Какие узлы показывает вкладка. Вкладки 4 (поиск) — пустой список, карта не привязана ни к одному из трёх графов. */
function tabGraphNodes(data: GraphData, tab: TabId): GraphNode[] {
  switch (tab) {
    case 1:
      return data.authors;
    case 2:
      return data.repos;
    case 3:
      return data.pubs;
    default:
      return [];
  }
}

/** Какие рёбра показывает вкладка — см. tabGraphNodes(), тот же принцип. */
function tabGraphEdges(data: GraphData, tab: TabId): Edge[] {
  switch (tab) {
    case 1:
      return data.coauth_edges;
    case 2:
      return data.repo_edges;
    case 3:
      return data.pub_edges;
    default:
      return [];
  }
}

/**
 * Цвет узла — цвет его департамента. Пока без приглушения/hover-состояний
 * (core/colors.ts с этой логикой — отдельный следующий шаг), только базовая
 * раскраска, чтобы точки на карте были различимы по департаментам.
 */
export function buildNodeFeatures(data: GraphData, lang: Lang, tab: TabId): FeatureCollection<Point, NodeProps> {
  // Map по id департамента, а не поиск в массиве на каждый узел —
  // департаментов немного, но узлов может быть тысячи.
  const deptById = new Map(data.departments.map((dept) => [dept.id, dept]));

  const features: Feature<Point, NodeProps>[] = tabGraphNodes(data, tab).map((node) => ({
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
 */
export function buildEdgeFeatures(data: GraphData, tab: TabId): FeatureCollection<LineString, EdgeProps> {
  const posByKey = new Map(allNodes(data).map((node) => [node.key, [node.gx, node.gy] as [number, number]]));

  const features: Feature<LineString, EdgeProps>[] = [];
  for (const edge of tabGraphEdges(data, tab)) {
    const s = posByKey.get(edge.s);
    const t = posByKey.get(edge.t);
    // Ребро без одной из позиций значит, что второй конец не попал в
    // authors/repos/pubs (например, отфильтрован раньше) — такое ребро
    // рисовать некуда, молча пропускаем, а не падаем с ошибкой.
    if (!s || !t) continue;

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
// Не экспортируем ID источников наружу — единственный код, которому они
// нужны, это refreshGraphForTab() ниже, в этом же файле.
const NODE_SOURCE_ID = "graph-nodes";
const EDGE_SOURCE_ID = "graph-edges";

// Ключ узла в GeoJSON-свойствах никогда не бывает пустой строкой (это
// либо OpenAlex-подобный id автора/публикации, либо repo-ключ) — поэтому
// пустая строка безопасно используется как "ничего не выбрано": ни одна
// настоящая точка на карте с ней не совпадёт.
const NO_SELECTION = "";

/**
 * Одно и то же MapLibre-выражение "если это выбранная точка — value1,
 * иначе — value2" нужно и при первой отрисовке (mountGraphLayers), и при
 * каждой смене выбора (setSelectedNode) — раньше оба места писали это
 * выражение и оба числа (8/4, 2/1) заново, что уже привело к рассинхрону
 * при правке. Теперь оба вызывающих места используют одну функцию и одни
 * значения из MAP_CONFIG — сменить размер выбранного узла можно в одном месте.
 */
function selectedNodeExpression(
  compareTo: string,
  selectedValue: number,
  defaultValue: number,
): ExpressionSpecification {
  return ["case", ["==", ["get", "key"], compareTo], selectedValue, defaultValue];
}

/**
 * Добавляет узлы и рёбра текущей вкладки на карту как источники и слои.
 * Сам клик (выбор узла/ребра) собирается отдельно в features/selection.ts —
 * эта функция только рисует, ничего не слушает. Вызывать один раз после
 * map.on("load", ...); дальнейшие смены вкладки/языка — через
 * refreshGraphForTab() ниже, не через повторный вызов этой функции
 * (addSource/addLayer второй раз на те же id упадёт с ошибкой).
 */
export function mountGraphLayers(map: MapLibreMap, data: GraphData, lang: Lang, tab: TabId): void {
  map.addSource(EDGE_SOURCE_ID, { type: "geojson", data: buildEdgeFeatures(data, tab) });
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

  map.addSource(NODE_SOURCE_ID, { type: "geojson", data: buildNodeFeatures(data, lang, tab) });
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
 * Пересобирает узлы и рёбра под новую вкладку и/или язык — вызывать при
 * смене store.tab или store.lang. Заменяет прежний refreshNodeLabels():
 * теперь один и тот же вызов нужен и для языка, и для смены графа, так
 * что незачем было держать две отдельные функции обновления.
 */
export function refreshGraphForTab(map: MapLibreMap, data: GraphData, lang: Lang, tab: TabId): void {
  const nodeSource = map.getSource(NODE_SOURCE_ID) as GeoJSONSource | undefined;
  nodeSource?.setData(buildNodeFeatures(data, lang, tab));

  const edgeSource = map.getSource(EDGE_SOURCE_ID) as GeoJSONSource | undefined;
  edgeSource?.setData(buildEdgeFeatures(data, tab));
}
