import type { GeoJSONSource, Map as MapLibreMap } from "maplibre-gl";
import { describe, expect, it, vi } from "vitest";
import type { SearchDetail } from "../src/contracts/search";
import { MAP_CONFIG } from "../src/core/config";
import { loadSampleGraphData, loadSampleSearchDetails, indexSearchDetailsByKey } from "../src/core/data";
import { Store, type AppState } from "../src/core/state";
import {
  buildEdgeFeatures,
  buildNodeFeatures,
  EDGE_LAYER_ID,
  mountReactiveGraph,
  NODE_LAYER_ID,
  nodeBounds,
  setSelectedEdge,
  setSelectedNode,
} from "../src/map/build";

// Пороги, которые ничего не отсекают — для тестов, где фильтрация не в фокусе.
const NO_FILTER = { minCoauth: 1, minSharedAuthors: 1, yearMax: 2026 };
// Большинство тестов здесь не про названия публикаций — пустая карта
// оставляет nodeLabel() на старом поведении (заглушка — ключ публикации).
const NO_SEARCH_DETAILS = new Map<string, SearchDetail>();

function initialState(overrides: Partial<AppState> = {}): AppState {
  return {
    tab: 1,
    lang: "ru",
    selection: null,
    filters: NO_FILTER,
    ...overrides,
  };
}

describe("map/build на фикстур-данных", () => {
  it("buildNodeFeatures отдаёт только узлы вкладки, а не всех сущностей сразу", async () => {
    const data = await loadSampleGraphData();

    // Три разных графа — авторы/репозитории/публикации не смешиваются в одной вкладке.
    expect(buildNodeFeatures(data, "ru", 1, NO_FILTER, NO_SEARCH_DETAILS).features).toHaveLength(data.authors.length);
    expect(buildNodeFeatures(data, "ru", 2, NO_FILTER, NO_SEARCH_DETAILS).features).toHaveLength(data.repos.length);
    expect(buildNodeFeatures(data, "ru", 3, NO_FILTER, NO_SEARCH_DETAILS).features).toHaveLength(data.pubs.length);
    // Вкладка 4 (поиск) не привязана ни к одному из трёх графов — карта пуста.
    expect(buildNodeFeatures(data, "ru", 4, NO_FILTER, NO_SEARCH_DETAILS).features).toHaveLength(0);
  });

  it("buildNodeFeatures красит узлы цветом их департамента", async () => {
    const data = await loadSampleGraphData();
    const fc = buildNodeFeatures(data, "ru", 1, NO_FILTER, NO_SEARCH_DETAILS);
    const deptColor = new Map(data.departments.map((d) => [d.id, d.color]));

    for (const feature of fc.features) {
      const author = data.authors.find((a) => a.key === feature.properties.key);
      expect(feature.properties.color).toBe(deptColor.get(author?.dept ?? -1));
    }
  });

  it("buildNodeFeatures подставляет настоящее название публикации из searchDetails вместо ключа", async () => {
    const data = await loadSampleGraphData();
    const searchDetails = indexSearchDetailsByKey(await loadSampleSearchDetails());

    const fc = buildNodeFeatures(data, "ru", 3, NO_FILTER, searchDetails);
    for (const feature of fc.features) {
      const detail = searchDetails.get(feature.properties.key);
      expect(feature.properties.label).toBe(detail?.label);
      expect(feature.properties.label).not.toBe(feature.properties.key);
    }
  });

  it("buildEdgeFeatures отдаёт рёбра только своей вкладки", async () => {
    const data = await loadSampleGraphData();

    expect(buildEdgeFeatures(data, 1, NO_FILTER).features).toHaveLength(data.coauth_edges.length);
    expect(buildEdgeFeatures(data, 2, NO_FILTER).features).toHaveLength(data.repo_edges.length);
    expect(buildEdgeFeatures(data, 3, NO_FILTER).features).toHaveLength(data.pub_edges.length);
    expect(buildEdgeFeatures(data, 4, NO_FILTER).features).toHaveLength(0);
  });

  it("buildEdgeFeatures пропускает рёбра без резолвящихся позиций", async () => {
    const data = await loadSampleGraphData();
    // Во фикстуре все s/t у coauth-рёбер существуют как узлы — ничего не отфильтровано.
    expect(buildEdgeFeatures(data, 1, NO_FILTER).features).toHaveLength(data.coauth_edges.length);
  });

  it("filters.minCoauth скрывает слабые связи соавторства на вкладке 1", async () => {
    const data = await loadSampleGraphData();
    const strong = data.coauth_edges.filter((e) => e.w >= 2).length;

    expect(buildEdgeFeatures(data, 1, { ...NO_FILTER, minCoauth: 2 }).features).toHaveLength(strong);
    expect(strong).toBeLessThan(data.coauth_edges.length); // проверка, что фикстура вообще даёт разброс весов
  });

  it("filters.minSharedAuthors скрывает слабые связи публикаций на вкладке 3", async () => {
    const data = await loadSampleGraphData();
    const strong = data.pub_edges.filter((e) => e.w >= 2).length;

    expect(buildEdgeFeatures(data, 3, { ...NO_FILTER, minSharedAuthors: 2 }).features).toHaveLength(strong);
  });

  it("filters.yearMax скрывает публикации позже указанного года, но не публикации без известного года", async () => {
    const data = await loadSampleGraphData();
    const filters = { ...NO_FILTER, yearMax: 2022 };
    const expectedPubs = data.pubs.filter((p) => p.year === null || p.year <= 2022);

    const nodeKeys = buildNodeFeatures(data, "ru", 3, filters, NO_SEARCH_DETAILS).features.map(
      (f) => f.properties.key,
    );
    expect(nodeKeys.sort()).toEqual(expectedPubs.map((p) => p.key).sort());
    expect(expectedPubs.some((p) => p.year === null)).toBe(true); // фикстура правда содержит пример без года

    // Ребро между публикациями, у одной из которых год скрыт фильтром, тоже пропадает.
    for (const feature of buildEdgeFeatures(data, 3, filters).features) {
      expect(nodeKeys).toContain(feature.properties.s);
      expect(nodeKeys).toContain(feature.properties.t);
    }
  });

  it("nodeBounds охватывает координаты всех узлов, а не только текущей вкладки", async () => {
    const data = await loadSampleGraphData();
    const [[minLon, minLat], [maxLon, maxLat]] = nodeBounds(data);
    const nodes = [...data.authors, ...data.repos, ...data.pubs];

    for (const node of nodes) {
      expect(node.gx).toBeGreaterThanOrEqual(minLon);
      expect(node.gx).toBeLessThanOrEqual(maxLon);
      expect(node.gy).toBeGreaterThanOrEqual(minLat);
      expect(node.gy).toBeLessThanOrEqual(maxLat);
    }
  });
});

describe("setSelectedNode", () => {
  function fakeMapWithPaint(): { map: MapLibreMap; setPaintProperty: ReturnType<typeof vi.fn> } {
    const setPaintProperty = vi.fn();
    return { map: { setPaintProperty } as unknown as MapLibreMap, setPaintProperty };
  }

  it("красит именно NODE_LAYER_ID (circle-radius и circle-stroke-width)", () => {
    const { map, setPaintProperty } = fakeMapWithPaint();
    setSelectedNode(map, "A1");

    expect(setPaintProperty).toHaveBeenCalledWith(NODE_LAYER_ID, "circle-radius", [
      "case",
      ["==", ["get", "key"], "A1"],
      MAP_CONFIG.node.radiusSelected,
      MAP_CONFIG.node.radius,
    ]);
    expect(setPaintProperty).toHaveBeenCalledWith(NODE_LAYER_ID, "circle-stroke-width", [
      "case",
      ["==", ["get", "key"], "A1"],
      MAP_CONFIG.node.strokeWidthSelected,
      MAP_CONFIG.node.strokeWidth,
    ]);
  });

  it("null снимает выделение — сравнение с пустой строкой не совпадёт ни с одним настоящим ключом", () => {
    const { map, setPaintProperty } = fakeMapWithPaint();
    setSelectedNode(map, null);

    expect(setPaintProperty).toHaveBeenCalledWith(NODE_LAYER_ID, "circle-radius", [
      "case",
      ["==", ["get", "key"], ""],
      MAP_CONFIG.node.radiusSelected,
      MAP_CONFIG.node.radius,
    ]);
  });
});

describe("setSelectedEdge", () => {
  function fakeMapWithPaint(): { map: MapLibreMap; setPaintProperty: ReturnType<typeof vi.fn> } {
    const setPaintProperty = vi.fn();
    return { map: { setPaintProperty } as unknown as MapLibreMap, setPaintProperty };
  }

  it("красит именно EDGE_LAYER_ID (line-width и line-opacity), а не слой узлов", () => {
    const { map, setPaintProperty } = fakeMapWithPaint();
    setSelectedEdge(map, { s: "A1", t: "A2" });

    expect(setPaintProperty).toHaveBeenCalledWith(EDGE_LAYER_ID, "line-width", [
      "case",
      ["all", ["==", ["get", "s"], "A1"], ["==", ["get", "t"], "A2"]],
      MAP_CONFIG.edge.widthSelected,
      MAP_CONFIG.edge.width,
    ]);
    expect(setPaintProperty).toHaveBeenCalledWith(EDGE_LAYER_ID, "line-opacity", [
      "case",
      ["all", ["==", ["get", "s"], "A1"], ["==", ["get", "t"], "A2"]],
      MAP_CONFIG.edge.opacitySelected,
      MAP_CONFIG.edge.opacity,
    ]);
  });

  it("null снимает выделение — сравнение с пустой строкой не совпадёт ни с одним настоящим s/t", () => {
    const { map, setPaintProperty } = fakeMapWithPaint();
    setSelectedEdge(map, null);

    expect(setPaintProperty).toHaveBeenCalledWith(EDGE_LAYER_ID, "line-width", [
      "case",
      ["all", ["==", ["get", "s"], ""], ["==", ["get", "t"], ""]],
      MAP_CONFIG.edge.widthSelected,
      MAP_CONFIG.edge.width,
    ]);
  });
});

describe("mountReactiveGraph", () => {
  function fakeMapWithSource(): { map: MapLibreMap; setData: ReturnType<typeof vi.fn> } {
    const setData = vi.fn();
    const map = {
      addSource: vi.fn(),
      addLayer: vi.fn(),
      getSource: () => ({ setData }) as unknown as GeoJSONSource,
    } as unknown as MapLibreMap;
    return { map, setData };
  }

  it("рисует граф один раз при монтировании и пересобирает его при смене tab/lang/filters, но не при смене selection", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    const { map, setData } = fakeMapWithSource();

    mountReactiveGraph(map, store, data, NO_SEARCH_DETAILS);
    expect(map.addSource).toHaveBeenCalledTimes(2); // узлы + рёбра
    expect(setData).not.toHaveBeenCalled();

    store.set({ selection: { kind: "node", key: "A1" } });
    expect(setData).not.toHaveBeenCalled(); // выбор — не повод пересобирать граф

    store.set({ tab: 2 });
    expect(setData).toHaveBeenCalledTimes(2); // узлы + рёбра пересобраны под новую вкладку

    store.set({ lang: "en" });
    expect(setData).toHaveBeenCalledTimes(4);

    store.set({ filters: { ...store.get().filters, minCoauth: 5 } });
    expect(setData).toHaveBeenCalledTimes(6);
  });
});
