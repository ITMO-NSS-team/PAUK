// Слой "map" — превращает GraphData в GeoJSON и рисует его на MapLibre.
// Логика интерпретации данных (какая подпись у узла, как искать узел по
// ключу) живёт в core/data.ts — этот файл только про геометрию и слои
// карты, ничего не решает про сами данные.

import type { Feature, FeatureCollection, LineString, Point } from "geojson";
import type { GeoJSONSource, Map as MapLibreMap } from "maplibre-gl";
import type { AuthorNode, Edge, GraphData, PubNode, RepoNode } from "../contracts/graph";
import { nodeLabel } from "../core/data";
import type { Lang } from "../core/i18n";

type GraphNode = AuthorNode | RepoNode | PubNode;

/** Свойства, которые кладутся в каждую GeoJSON-точку узла — доступны в paint-выражениях слоя через ["get", "имя"]. */
interface NodeProps {
  key: string;
  kind: GraphNode["kind"];
  label: string;
  color: string;
}

/** Свойства ребра: s/t — ключи узлов на концах (нужны, чтобы построить Selection по клику), w — вес (для будущей толщины линии, сейчас линия одной толщины). */
interface EdgeProps {
  s: string;
  t: string;
  w: number;
}

/** Все три вида узлов в одном списке — раздельная отрисовка по kind делает уже слой карты, не эта функция. */
function allNodes(data: GraphData): GraphNode[] {
  return [...data.authors, ...data.repos, ...data.pubs];
}

/**
 * Цвет узла — цвет его департамента. Пока без приглушения/hover-состояний
 * (core/colors.ts с этой логикой — отдельный следующий шаг), только базовая
 * раскраска, чтобы точки на карте были различимы по департаментам.
 */
export function buildNodeFeatures(data: GraphData, lang: Lang): FeatureCollection<Point, NodeProps> {
  // Map по id департамента, а не поиск в массиве на каждый узел —
  // департаментов немного, но узлов может быть тысячи.
  const deptById = new Map(data.departments.map((dept) => [dept.id, dept]));

  const features: Feature<Point, NodeProps>[] = allNodes(data).map((node) => ({
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
      color: deptById.get(node.dept)?.color ?? "#9d9d9d",
    },
  }));

  return { type: "FeatureCollection", features };
}

/**
 * Рёбра — только те, для которых есть обе позиции. Связи репозиторий-автор
 * и репозиторий-публикация без веса (w) сюда не входят — это отдельные
 * визуальные слои, добавим при необходимости.
 */
export function buildEdgeFeatures(data: GraphData): FeatureCollection<LineString, EdgeProps> {
  const posByKey = new Map(allNodes(data).map((node) => [node.key, [node.gx, node.gy] as [number, number]]));

  const weightedEdges: Edge[] = [...data.coauth_edges, ...data.repo_edges, ...data.pub_edges];

  const features: Feature<LineString, EdgeProps>[] = [];
  for (const edge of weightedEdges) {
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
 * Прямоугольник, охватывающий все узлы — координаты gx/gy не привязаны
 * к реальной географии и их диапазон зависит от раскладки (у фикстуры он
 * крошечный, у настоящих данных — намного больше), поэтому карту нужно
 * подстраивать под данные явно, а не полагаться на фиксированный zoom.
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
// Не экспортируем ID источников наружу — единственный код, которому они
// нужны, это refreshNodeLabels() ниже, в этом же файле.
const NODE_SOURCE_ID = "graph-nodes";
const EDGE_SOURCE_ID = "graph-edges";

// Ключ узла в GeoJSON-свойствах никогда не бывает пустой строкой (это
// либо OpenAlex-подобный id автора/публикации, либо repo-ключ) — поэтому
// пустая строка безопасно используется как "ничего не выбрано": ни одна
// настоящая точка на карте с ней не совпадёт.
const NO_SELECTION = "";

/**
 * Добавляет узлы и рёбра графа на карту как источники и слои. Сам клик
 * (выбор узла) собирается отдельно в features/selection.ts — эта функция
 * только рисует, ничего не слушает. Вызывать один раз после map.on("load", ...).
 */
export function mountGraphLayers(map: MapLibreMap, data: GraphData, lang: Lang): void {
  map.addSource(EDGE_SOURCE_ID, { type: "geojson", data: buildEdgeFeatures(data) });
  map.addLayer({
    id: EDGE_LAYER_ID,
    type: "line",
    source: EDGE_SOURCE_ID,
    paint: { "line-color": "#9d9d9d", "line-width": 0.6, "line-opacity": 0.5 },
  });

  map.addSource(NODE_SOURCE_ID, { type: "geojson", data: buildNodeFeatures(data, lang) });
  map.addLayer({
    id: NODE_LAYER_ID,
    type: "circle",
    source: NODE_SOURCE_ID,
    paint: {
      // MapLibre "case"-выражение: сравниваем properties.key текущей точки
      // с выбранным ключом (setSelectedNode ниже подставляет его сюда через
      // setPaintProperty) и рисуем крупнее с более толстой обводкой именно
      // выбранный узел, остальные — как обычно. Изначально не выбрано ничего.
      "circle-radius": ["case", ["==", ["get", "key"], NO_SELECTION], 8, 4],
      "circle-color": ["get", "color"],
      "circle-stroke-width": ["case", ["==", ["get", "key"], NO_SELECTION], 2, 1],
      "circle-stroke-color": "#ffffff",
    },
  });
}

/**
 * Подсвечивает выбранный узел на карте (крупнее, с более толстой обводкой)
 * и снимает подсветку с остальных. key === null — снять выделение совсем.
 */
export function setSelectedNode(map: MapLibreMap, key: string | null): void {
  const compareTo = key ?? NO_SELECTION;
  map.setPaintProperty(NODE_LAYER_ID, "circle-radius", ["case", ["==", ["get", "key"], compareTo], 8, 4]);
  map.setPaintProperty(NODE_LAYER_ID, "circle-stroke-width", ["case", ["==", ["get", "key"], compareTo], 2, 1]);
}

/**
 * Пересобирает properties.label узлов под новый язык — вызывается при
 * переключении lang. Свойство label сейчас нигде визуально не
 * используется (подписи на самой карте — overlay.ts, отдельный будущий
 * шаг), но держать его в актуальном состоянии дешевле, чем потом
 * вспоминать, что источник этого свойства мог протухнуть при смене языка.
 */
export function refreshNodeLabels(map: MapLibreMap, data: GraphData, lang: Lang): void {
  const source = map.getSource(NODE_SOURCE_ID) as GeoJSONSource | undefined;
  source?.setData(buildNodeFeatures(data, lang));
}
