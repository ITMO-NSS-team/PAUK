import Graph from "graphology";
import type Sigma from "sigma";
import { describe, expect, it, vi } from "vitest";
import type { PubDetail } from "../src/contracts/graph";
import { MAP_CONFIG } from "../src/core/config";
import { indexDetailsByKey } from "../src/core/data";
import { Store, type AppState } from "../src/core/state";
import { loadSampleGraphData, loadSamplePubDetails } from "./fixtures";
import { mountReactiveGraph, populateGraph } from "../src/map/build";

// Пороги, которые ничего не отсекают — для тестов, где фильтрация не в фокусе.
const NO_FILTER = { minCoauth: 1, minSharedAuthors: 1, yearMax: 2026 };
// Большинство тестов здесь не про названия публикаций — пустая карта
// оставляет nodeLabel() на старом поведении (заглушка — ключ публикации).
const NO_PUB_DETAILS = new Map<string, PubDetail>();

function initialState(overrides: Partial<AppState> = {}): AppState {
  return {
    tab: 1,
    lang: "ru",
    selection: null,
    filters: NO_FILTER,
    ...overrides,
  };
}

describe("populateGraph на фикстур-данных", () => {
  it("отдаёт только узлы вкладки, а не всех сущностей сразу", async () => {
    const data = await loadSampleGraphData();

    // Три разных графа — авторы/репозитории/публикации не смешиваются в одной вкладке.
    const authorsGraph = new Graph();
    populateGraph(authorsGraph, data, "ru", 1, NO_FILTER, NO_PUB_DETAILS);
    expect(authorsGraph.order).toBe(data.authors.length);

    const reposGraph = new Graph();
    populateGraph(reposGraph, data, "ru", 2, NO_FILTER, NO_PUB_DETAILS);
    expect(reposGraph.order).toBe(data.repos.length);

    const pubsGraph = new Graph();
    populateGraph(pubsGraph, data, "ru", 3, NO_FILTER, NO_PUB_DETAILS);
    expect(pubsGraph.order).toBe(data.pubs.length);

    // Вкладка 4 (поиск) не привязана ни к одному из трёх графов — граф пуст.
    const searchGraph = new Graph();
    populateGraph(searchGraph, data, "ru", 4, NO_FILTER, NO_PUB_DETAILS);
    expect(searchGraph.order).toBe(0);
  });

  it("красит узлы цветом их департамента", async () => {
    const data = await loadSampleGraphData();
    const graph = new Graph();
    populateGraph(graph, data, "ru", 1, NO_FILTER, NO_PUB_DETAILS);
    const deptColor = new Map(data.departments.map((d) => [d.id, d.color]));

    for (const key of graph.nodes()) {
      const author = data.authors.find((a) => a.key === key);
      expect(graph.getNodeAttribute(key, "color")).toBe(deptColor.get(author?.dept ?? -1));
    }
  });

  it("подставляет настоящее название публикации из pubDetails вместо ключа", async () => {
    const data = await loadSampleGraphData();
    const pubDetails = indexDetailsByKey(await loadSamplePubDetails());
    const graph = new Graph();
    populateGraph(graph, data, "ru", 3, NO_FILTER, pubDetails);

    for (const key of graph.nodes()) {
      const detail = pubDetails.get(key);
      expect(graph.getNodeAttribute(key, "label")).toBe(detail?.label);
      expect(graph.getNodeAttribute(key, "label")).not.toBe(key);
    }
  });

  it("отдаёт рёбра только своей вкладки", async () => {
    const data = await loadSampleGraphData();

    const authorsGraph = new Graph();
    populateGraph(authorsGraph, data, "ru", 1, NO_FILTER, NO_PUB_DETAILS);
    expect(authorsGraph.size).toBe(data.coauth_edges.length);

    const reposGraph = new Graph();
    populateGraph(reposGraph, data, "ru", 2, NO_FILTER, NO_PUB_DETAILS);
    expect(reposGraph.size).toBe(data.repo_edges.length);

    const pubsGraph = new Graph();
    populateGraph(pubsGraph, data, "ru", 3, NO_FILTER, NO_PUB_DETAILS);
    expect(pubsGraph.size).toBe(data.pub_edges.length);

    const searchGraph = new Graph();
    populateGraph(searchGraph, data, "ru", 4, NO_FILTER, NO_PUB_DETAILS);
    expect(searchGraph.size).toBe(0);
  });

  it("filters.minCoauth скрывает слабые связи соавторства на вкладке 1", async () => {
    const data = await loadSampleGraphData();
    const strong = data.coauth_edges.filter((e) => e.w >= 2).length;

    const graph = new Graph();
    populateGraph(graph, data, "ru", 1, { ...NO_FILTER, minCoauth: 2 }, NO_PUB_DETAILS);
    expect(graph.size).toBe(strong);
    expect(strong).toBeLessThan(data.coauth_edges.length); // проверка, что фикстура вообще даёт разброс весов
  });

  it("filters.minSharedAuthors скрывает слабые связи публикаций на вкладке 3", async () => {
    const data = await loadSampleGraphData();
    const strong = data.pub_edges.filter((e) => e.w >= 2).length;

    const graph = new Graph();
    populateGraph(graph, data, "ru", 3, { ...NO_FILTER, minSharedAuthors: 2 }, NO_PUB_DETAILS);
    expect(graph.size).toBe(strong);
  });

  it("filters.yearMax скрывает публикации позже указанного года, но не публикации без известного года", async () => {
    const data = await loadSampleGraphData();
    const filters = { ...NO_FILTER, yearMax: 2022 };
    const expectedPubs = data.pubs.filter((p) => p.year === null || p.year <= 2022);

    const graph = new Graph();
    populateGraph(graph, data, "ru", 3, filters, NO_PUB_DETAILS);
    expect(graph.nodes().sort()).toEqual(expectedPubs.map((p) => p.key).sort());
    expect(expectedPubs.some((p) => p.year === null)).toBe(true); // фикстура правда содержит пример без года

    // Ребро между публикациями, у одной из которых год скрыт фильтром, тоже пропадает —
    // graph.hasNode() внутри populateGraph уже отражает фильтр узлов, отдельной
    // проверки года на рёбрах не нужно (в отличие от MapLibre-версии).
    for (const edgeKey of graph.edges()) {
      const [s, t] = graph.extremities(edgeKey);
      expect(graph.nodes()).toContain(s);
      expect(graph.nodes()).toContain(t);
    }
  });
});

describe("applySelectionHighlighting (через mountReactiveGraph)", () => {
  /**
   * Фейковый Sigma-рендерер: хранит реальный graphology.Graph (для
   * extremities/getEdgeAttribute внутри reducer'ов) и перехватывает
   * setSetting("nodeReducer"/"edgeReducer", ...), чтобы тест мог вызвать
   * сохранённый reducer напрямую — настоящий Sigma в jsdom не поднять
   * (нужен WebGL-канвас), поэтому мы никогда не конструируем его в тестах.
   */
  // any, не never[] — это фейк для перехвата колбэков в тесте, а не
  // рабочий код, где важна строгая типизация аргументов.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  type Reducer = (...args: any[]) => unknown;

  function fakeRenderer(graph: Graph): { renderer: Sigma; getReducer: (key: "nodeReducer" | "edgeReducer") => Reducer } {
    const reducers = new Map<string, Reducer>();
    const renderer = {
      getGraph: () => graph,
      setSetting: (key: string, value: unknown) => reducers.set(key, value as Reducer),
      refresh: vi.fn(),
    } as unknown as Sigma;
    return {
      renderer,
      getReducer: (key) => {
        const reducer = reducers.get(key);
        if (!reducer) throw new Error(`reducer "${key}" ещё не зарегистрирован`);
        return reducer;
      },
    };
  }

  it("nodeReducer подсвечивает именно выбранный узел", async () => {
    const data = await loadSampleGraphData();
    const graph = new Graph();
    populateGraph(graph, data, "ru", 1, NO_FILTER, NO_PUB_DETAILS);
    const store = new Store<AppState>(initialState({ selection: { kind: "node", key: "A1" } }));
    const { renderer, getReducer } = fakeRenderer(graph);

    mountReactiveGraph(renderer, store, data, NO_PUB_DETAILS);
    const nodeReducer = getReducer("nodeReducer");

    const baseData = { x: 0, y: 0, size: MAP_CONFIG.node.radius, color: "#fff", label: "" };
    expect(nodeReducer("A1", baseData)).toMatchObject({ highlighted: true, size: MAP_CONFIG.node.radiusSelected });
    expect(nodeReducer("A2", baseData)).toBe(baseData); // не выбран — reducer возвращает данные как есть
  });

  it("edgeReducer подсвечивает ребро независимо от порядка s/t в selection", async () => {
    const data = await loadSampleGraphData();
    const graph = new Graph();
    graph.addNode("A1", { x: 0, y: 0 });
    graph.addNode("A2", { x: 1, y: 1 });
    graph.addEdge("A1", "A2", { weight: 3 });
    const store = new Store<AppState>(initialState({ selection: { kind: "edge", s: "A2", t: "A1", w: 3 } }));
    const { renderer, getReducer } = fakeRenderer(graph);

    mountReactiveGraph(renderer, store, data, NO_PUB_DETAILS);
    const edgeReducer = getReducer("edgeReducer");
    const [edgeKey] = graph.edges();
    if (!edgeKey) throw new Error("граф должен содержать хотя бы одно ребро");

    const baseData = { size: MAP_CONFIG.edge.width, color: MAP_CONFIG.edge.color, label: null, hidden: false, forceLabel: false, zIndex: 0, type: "line" };
    expect(edgeReducer(edgeKey, baseData)).toMatchObject({
      color: MAP_CONFIG.edge.colorSelected,
      size: MAP_CONFIG.edge.widthSelected,
    });
  });
});

describe("mountReactiveGraph", () => {
  function fakeRenderer(graph: Graph): { renderer: Sigma; refresh: ReturnType<typeof vi.fn> } {
    const refresh = vi.fn();
    const renderer = {
      getGraph: () => graph,
      setSetting: vi.fn(),
      refresh,
    } as unknown as Sigma;
    return { renderer, refresh };
  }

  it("не пересобирает граф при монтировании (он уже наполнен снаружи) и пересобирает при смене tab/lang/filters, но не при смене selection", async () => {
    const data = await loadSampleGraphData();
    const graph = new Graph();
    populateGraph(graph, data, "ru", 1, NO_FILTER, NO_PUB_DETAILS);
    const store = new Store<AppState>(initialState());
    const { renderer, refresh } = fakeRenderer(graph);

    mountReactiveGraph(renderer, store, data, NO_PUB_DETAILS);
    expect(graph.order).toBe(data.authors.length);

    store.set({ selection: { kind: "node", key: "A1" } });
    expect(refresh).toHaveBeenCalledTimes(1); // выбор — лёгкий refresh(), не пересборка
    expect(graph.order).toBe(data.authors.length); // граф не пересобирался

    store.set({ tab: 2 });
    expect(graph.order).toBe(data.repos.length); // пересобран под новую вкладку

    store.set({ lang: "en" });
    expect(graph.order).toBe(data.repos.length); // та же вкладка, граф пересобран заново (не упал)

    store.set({ filters: { ...store.get().filters, minCoauth: 5 } });
    expect(refresh).toHaveBeenCalledTimes(1); // ни одно из трёх пересобраний refresh() не дёргало
  });
});
