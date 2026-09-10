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

// any, не never[] — это фейк для перехвата колбэков в тесте, а не рабочий
// код, где важна строгая типизация аргументов.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Reducer = (...args: any[]) => unknown;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type EventHandler = (...args: any[]) => void;

/**
 * Фейковый Sigma-рендерер: хранит реальный graphology.Graph (нужен
 * reducer'ам для extremities/getEdgeAttribute/areNeighbors) и перехватывает
 * setSetting(...)/on(...), чтобы тест мог вызвать сохранённые колбэки
 * напрямую — настоящий Sigma в jsdom не поднять (нужен WebGL-канвас).
 */
function fakeRenderer(graph: Graph): {
  renderer: Sigma;
  refresh: ReturnType<typeof vi.fn>;
  getReducer: (key: "nodeReducer" | "edgeReducer") => Reducer;
  fire: (event: string, payload?: unknown) => void;
} {
  const reducers = new Map<string, Reducer>();
  const handlers = new Map<string, EventHandler>();
  const refresh = vi.fn();
  const renderer = {
    getGraph: () => graph,
    setSetting: (key: string, value: unknown) => reducers.set(key, value as Reducer),
    on: (event: string, cb: EventHandler) => handlers.set(event, cb),
    off: vi.fn(),
    refresh,
  } as unknown as Sigma;

  return {
    renderer,
    refresh,
    getReducer: (key) => {
      const reducer = reducers.get(key);
      if (!reducer) throw new Error(`reducer "${key}" ещё не зарегистрирован`);
      return reducer;
    },
    fire: (event, payload) => handlers.get(event)?.(payload),
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

describe("applyHighlighting (через mountReactiveGraph) — выбор/наведение и притухание соседей", () => {
  const NODE_BASE = { x: 0, y: 0, size: MAP_CONFIG.node.radius, color: "#fff", label: "L" };
  const EDGE_BASE = {
    size: MAP_CONFIG.edge.width,
    color: MAP_CONFIG.edge.color,
    label: null,
    hidden: false,
    forceLabel: false,
    zIndex: 0,
    type: "line",
  };

  it("nodeReducer подсвечивает выбранный узел, соседей оставляет как есть, остальных притушает", async () => {
    const data = await loadSampleGraphData();
    const graph = new Graph();
    graph.addNode("A1", { x: 0, y: 0 });
    graph.addNode("A2", { x: 1, y: 1 });
    graph.addNode("A3", { x: 2, y: 2 });
    graph.addEdge("A1", "A2");
    const store = new Store<AppState>(initialState({ selection: { kind: "node", key: "A1" } }));
    const { renderer, getReducer } = fakeRenderer(graph);

    mountReactiveGraph(renderer, store, data, NO_PUB_DETAILS);
    const nodeReducer = getReducer("nodeReducer");

    expect(nodeReducer("A1", NODE_BASE)).toMatchObject({ highlighted: true, size: MAP_CONFIG.node.radiusSelected });
    expect(nodeReducer("A2", NODE_BASE)).toMatchObject({ color: NODE_BASE.color, label: NODE_BASE.label }); // сосед — не тронут
    expect(nodeReducer("A3", NODE_BASE)).toMatchObject({ color: MAP_CONFIG.node.dimColor, label: "" }); // не сосед — притушен
  });

  it("без выбора и без наведения ничего не притушено", async () => {
    const data = await loadSampleGraphData();
    const graph = new Graph();
    graph.addNode("A1", { x: 0, y: 0 });
    graph.addNode("A2", { x: 1, y: 1 });
    const store = new Store<AppState>(initialState());
    const { renderer, getReducer } = fakeRenderer(graph);

    mountReactiveGraph(renderer, store, data, NO_PUB_DETAILS);
    expect(getReducer("nodeReducer")("A2", NODE_BASE)).toEqual(NODE_BASE);
  });

  it("наведение мышью (enterNode) временно становится фокусом вместо выбора, leaveNode его снимает", async () => {
    const data = await loadSampleGraphData();
    const graph = new Graph();
    graph.addNode("A1", { x: 0, y: 0 });
    graph.addNode("A2", { x: 1, y: 1 });
    graph.addNode("A3", { x: 2, y: 2 });
    graph.addEdge("A2", "A3");
    const store = new Store<AppState>(initialState()); // ничего не выбрано кликом
    const { renderer, getReducer, fire } = fakeRenderer(graph);

    mountReactiveGraph(renderer, store, data, NO_PUB_DETAILS);
    const nodeReducer = getReducer("nodeReducer");

    fire("enterNode", { node: "A2" });
    expect(nodeReducer("A3", NODE_BASE)).toMatchObject({ color: NODE_BASE.color }); // сосед наведённого A2 — не притушен
    expect(nodeReducer("A1", NODE_BASE)).toMatchObject({ color: MAP_CONFIG.node.dimColor, label: "" }); // не сосед — притушен

    fire("leaveNode", {});
    expect(nodeReducer("A1", NODE_BASE)).toEqual(NODE_BASE); // наведение снято — фокуса больше нет
  });

  it("edgeReducer притушает рёбра, не задевающие фокус, но не трогает задевающие", async () => {
    const data = await loadSampleGraphData();
    const graph = new Graph();
    graph.addNode("A1", { x: 0, y: 0 });
    graph.addNode("A2", { x: 1, y: 1 });
    graph.addNode("A3", { x: 2, y: 2 });
    graph.addEdge("A1", "A2");
    graph.addEdge("A2", "A3");
    const store = new Store<AppState>(initialState({ selection: { kind: "node", key: "A1" } }));
    const { renderer, getReducer } = fakeRenderer(graph);

    mountReactiveGraph(renderer, store, data, NO_PUB_DETAILS);
    const edgeReducer = getReducer("edgeReducer");

    const [touchingFocus] = graph.edges("A1", "A2");
    const [notTouchingFocus] = graph.edges("A2", "A3");
    if (!touchingFocus || !notTouchingFocus) throw new Error("граф должен содержать оба ребра");

    expect(edgeReducer(touchingFocus, EDGE_BASE)).toMatchObject({ color: EDGE_BASE.color });
    expect(edgeReducer(notTouchingFocus, EDGE_BASE)).toMatchObject({ color: MAP_CONFIG.edge.colorDimmed });
  });

  it("edgeReducer подсвечивает выбранное ребро независимо от порядка s/t — притухание сюда не примешивается (фокус только для selection.kind === node)", async () => {
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

    expect(edgeReducer(edgeKey, EDGE_BASE)).toMatchObject({
      color: MAP_CONFIG.edge.colorSelected,
      size: MAP_CONFIG.edge.widthSelected,
    });
  });
});

describe("mountReactiveGraph", () => {
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
