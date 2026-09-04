import type { Feature, FeatureCollection, LineString, Point } from "geojson";
import type { Map as MapLibreMap } from "maplibre-gl";
import type { AuthorNode, Edge, GraphData, PubNode, RepoNode } from "../contracts/graph";

type GraphNode = AuthorNode | RepoNode | PubNode;

interface NodeProps {
  key: string;
  kind: GraphNode["kind"];
  label: string;
  color: string;
}

interface EdgeProps {
  w: number;
}

/** Все три вида узлов в одном списке — раздельная отрисовка по kind делает уже слой карты, не эта функция. */
function allNodes(data: GraphData): GraphNode[] {
  return [...data.authors, ...data.repos, ...data.pubs];
}

/** У PubNode нет своего label (подпись публикации приходит отдельно, из SearchDetail) — используем key как заглушку. */
function nodeLabel(node: GraphNode): string {
  return "label" in node ? node.label : node.key;
}

/**
 * Цвет узла — цвет его департамента. Пока без приглушения/hover-состояний
 * (core/colors.ts с этой логикой — отдельный следующий шаг), только базовая
 * раскраска, чтобы точки на карте были различимы по департаментам.
 */
export function buildNodeFeatures(data: GraphData): FeatureCollection<Point, NodeProps> {
  const deptById = new Map(data.departments.map((dept) => [dept.id, dept]));

  const features: Feature<Point, NodeProps>[] = allNodes(data).map((node) => ({
    type: "Feature",
    geometry: { type: "Point", coordinates: [node.gx, node.gy] },
    properties: {
      key: node.key,
      kind: node.kind,
      label: nodeLabel(node),
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
    if (!s || !t) continue;

    features.push({
      type: "Feature",
      geometry: { type: "LineString", coordinates: [s, t] },
      properties: { w: edge.w },
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

const NODE_SOURCE_ID = "graph-nodes";
const EDGE_SOURCE_ID = "graph-edges";

/**
 * Добавляет узлы и рёбра графа на карту как источники и слои. Без
 * интерактивности (клик/hover/выбор) — это отдельный следующий шаг
 * (features/selection.ts). Вызывать один раз после map.on("load", ...).
 */
export function mountGraphLayers(map: MapLibreMap, data: GraphData): void {
  map.addSource(EDGE_SOURCE_ID, { type: "geojson", data: buildEdgeFeatures(data) });
  map.addLayer({
    id: "graph-edges-line",
    type: "line",
    source: EDGE_SOURCE_ID,
    paint: { "line-color": "#9d9d9d", "line-width": 0.6, "line-opacity": 0.5 },
  });

  map.addSource(NODE_SOURCE_ID, { type: "geojson", data: buildNodeFeatures(data) });
  map.addLayer({
    id: "graph-nodes-circle",
    type: "circle",
    source: NODE_SOURCE_ID,
    paint: {
      "circle-radius": 4,
      "circle-color": ["get", "color"],
      "circle-stroke-width": 1,
      "circle-stroke-color": "#ffffff",
    },
  });
}
