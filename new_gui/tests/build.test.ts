import Graph from "graphology";
import type Sigma from "sigma";
import { describe, expect, it, vi } from "vitest";
import type { GraphData, PubDetail } from "../src/contracts/graph";
import { MAP_CONFIG, NO_DEPT_COLOR } from "../src/core/config";
import { indexDetailsByKey } from "../src/core/data";
import { Store, type AppState } from "../src/core/state";
import { loadSampleGraphData, loadSamplePubDetails } from "./fixtures";
import { deptNodeKey, mountReactiveGraph, mountZoomDebug, parseDeptNodeKey, populateGraph } from "../src/map/build";

// Пороги, которые ничего не отсекают — для тестов, где фильтрация не в фокусе.
const NO_FILTER = {
  minCoauth: 1,
  minSharedAuthors: 1,
  yearMax: 2026,
  showNoDeptAuthors: true,
  showNoDeptPubs: true,
};
// Большинство тестов здесь не про названия публикаций — пустая карта
// оставляет nodeLabel() на старом поведении (заглушка — ключ публикации).
const NO_PUB_DETAILS = new Map<string, PubDetail>();

const NODE_BASE = { x: 0, y: 0, size: MAP_CONFIG.node.radius, color: "#fff", label: "L", hidden: false };
const EDGE_BASE = {
  size: MAP_CONFIG.edge.width,
  color: MAP_CONFIG.edge.color,
  label: null,
  hidden: false,
  forceLabel: false,
  zIndex: 0,
  type: "line",
};

function initialState(overrides: Partial<AppState> = {}): AppState {
  return {
    screen: "app",
    tab: 1,
    lang: "ru",
    selection: null,
    filters: NO_FILTER,
    ...overrides,
  };
}

/** Ключи только "реальных" узлов графа (авторы/репозитории/публикации) — без якорей подписей департаментов. */
function realNodeKeys(graph: Graph): string[] {
  return graph.nodes().filter((key) => parseDeptNodeKey(key) === null);
}

/** Ключи только "реальных" рёбер графа — без рёбер между департаментами. */
function realEdgeKeys(graph: Graph): string[] {
  return graph.edges().filter((edgeKey) => parseDeptNodeKey(graph.extremities(edgeKey)[0]) === null);
}

/**
 * Минимальный `GraphData` с одним обычным департаментом и одним
 * синтетическим "Без департамента" (`color === NO_DEPT_COLOR`) — общая
 * фикстура (`./fixtures.ts`) намеренно не трогается ради этих тестов (на
 * неё завязаны точные количества в других тестах этого файла), поэтому
 * тесты на фильтр строят собственные, изолированные данные.
 */
function dataWithNoDept(): GraphData {
  return {
    departments: [
      { id: 0, name: "ИТМО", name_en: "ITMO", color: "#c27070", n: 1, n_authors: 1, n_pubs: 1, n_repos: 0 },
      {
        id: 1,
        name: "Без департамента",
        name_en: "No department",
        color: NO_DEPT_COLOR,
        n: 1,
        n_authors: 1,
        n_pubs: 1,
        n_repos: 0,
      },
    ],
    dept_edges: [],
    authors: [
      { key: "A1", kind: "author", dept: 0, label: "Иванов", label_en: "Ivanov", pubs_count: 1, rank: 1, gx: 100, gy: 100 },
      { key: "A2", kind: "author", dept: 1, label: "Петров", label_en: "Petrov", pubs_count: 1, rank: 1, gx: 200, gy: 200 },
    ],
    coauth_edges: [],
    repos: [],
    repo_edges: [],
    repo_author_edges: [],
    repo_pub_edges: [],
    pubs: [
      { key: "P1", kind: "pub", dept: 0, depts: [0], year: 2024, n_authors: 1, rank: 1, gx: 100, gy: 100 },
      { key: "P2", kind: "pub", dept: 1, depts: [1], year: 2024, n_authors: 1, rank: 1, gx: 200, gy: 200 },
    ],
    pub_edges: [],
    all_edges: [],
  };
}

// any, не never[] — это фейк для перехвата колбэков в тесте, а не рабочий
// код, где важна строгая типизация аргументов.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Reducer = (...args: any[]) => unknown;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type EventHandler = (...args: any[]) => void;

/** См. fakeRenderer::getNodeDisplayData — сдвиг, доказывающий, что flyToSelection берёт координаты именно оттуда, а не из graph.getNodeAttributes(). */
const FRAMED_COORD_OFFSET = 1000;

/**
 * Фейковый Sigma-рендерер: хранит реальный graphology.Graph (нужен
 * reducer'ам для extremities/getEdgeAttribute/areNeighbors) и перехватывает
 * setSetting(...)/on(...)/camera.on(...), чтобы тест мог вызвать сохранённые
 * колбэки напрямую — настоящий Sigma в jsdom не поднять (нужен WebGL-канвас).
 */
function fakeRenderer(graph: Graph): {
  renderer: Sigma;
  refresh: ReturnType<typeof vi.fn>;
  cameraAnimate: ReturnType<typeof vi.fn>;
  getReducer: (key: "nodeReducer" | "edgeReducer") => Reducer;
  fire: (event: string, payload?: unknown) => void;
  fireCameraUpdated: (ratio: number) => void;
} {
  const reducers = new Map<string, Reducer>();
  const handlers = new Map<string, EventHandler>();
  const cameraHandlers = new Map<string, EventHandler>();
  const refresh = vi.fn();
  let cameraRatio = 1;

  const camera = {
    getState: () => ({ ratio: cameraRatio, x: 0, y: 0, angle: 0 }),
    on: (event: string, cb: EventHandler) => cameraHandlers.set(event, cb),
    off: vi.fn(),
    animate: vi.fn(),
  };

  const renderer = {
    getGraph: () => graph,
    getCamera: () => camera,
    setSetting: (key: string, value: unknown) => reducers.set(key, value as Reducer),
    on: (event: string, cb: EventHandler) => handlers.set(event, cb),
    off: vi.fn(),
    refresh,
    // Намеренно ДРУГИЕ координаты, чем "сырые" graph.getNodeAttributes() —
    // так же, как настоящая Sigma отдаёт из getNodeDisplayData() координаты
    // ПОСЛЕ normalizationFunction, а не сырые атрибуты графа (см.
    // map/build.ts::flyToSelection). Если код по ошибке снова начнёт читать
    // graph.getNodeAttributes() напрямую, тест ниже это поймает.
    getNodeDisplayData: (key: string) => {
      if (!graph.hasNode(key)) return undefined;
      const attrs = graph.getNodeAttributes(key);
      return { ...attrs, x: attrs.x + FRAMED_COORD_OFFSET, y: attrs.y + FRAMED_COORD_OFFSET };
    },
  } as unknown as Sigma;

  return {
    renderer,
    refresh,
    cameraAnimate: camera.animate,
    getReducer: (key) => {
      const reducer = reducers.get(key);
      if (!reducer) throw new Error(`reducer "${key}" ещё не зарегистрирован`);
      return reducer;
    },
    fire: (event, payload) => handlers.get(event)?.(payload),
    fireCameraUpdated: (ratio) => {
      cameraRatio = ratio;
      cameraHandlers.get("updated")?.({ ratio, x: 0, y: 0, angle: 0 });
    },
  };
}

describe("populateGraph на фикстур-данных", () => {
  it("отдаёт только узлы вкладки, а не всех сущностей сразу", async () => {
    const data = await loadSampleGraphData();

    // Три разных графа — авторы/репозитории/публикации не смешиваются в одной вкладке.
    const authorsGraph = new Graph();
    populateGraph(authorsGraph, data, "ru", 1, NO_FILTER, NO_PUB_DETAILS);
    expect(realNodeKeys(authorsGraph)).toHaveLength(data.authors.length);

    const reposGraph = new Graph();
    populateGraph(reposGraph, data, "ru", 2, NO_FILTER, NO_PUB_DETAILS);
    expect(realNodeKeys(reposGraph)).toHaveLength(data.repos.length);

    const pubsGraph = new Graph();
    populateGraph(pubsGraph, data, "ru", 3, NO_FILTER, NO_PUB_DETAILS);
    expect(realNodeKeys(pubsGraph)).toHaveLength(data.pubs.length);
  });

  it("красит узлы цветом их департамента", async () => {
    const data = await loadSampleGraphData();
    const graph = new Graph();
    populateGraph(graph, data, "ru", 1, NO_FILTER, NO_PUB_DETAILS);
    const deptColor = new Map(data.departments.map((d) => [d.id, d.color]));

    for (const key of realNodeKeys(graph)) {
      const author = data.authors.find((a) => a.key === key);
      expect(graph.getNodeAttribute(key, "color")).toBe(deptColor.get(author?.dept ?? -1));
    }
  });

  it("подставляет настоящее название публикации из pubDetails вместо ключа (с учётом обрезки на карте, см. labelMaxLength)", async () => {
    const data = await loadSampleGraphData();
    const pubDetails = indexDetailsByKey(await loadSamplePubDetails());
    const graph = new Graph();
    populateGraph(graph, data, "ru", 3, NO_FILTER, pubDetails);

    for (const key of realNodeKeys(graph)) {
      const detail = pubDetails.get(key);
      const label = graph.getNodeAttribute(key, "label") as string;
      const fullTitle = detail?.label ?? "";
      expect(fullTitle.startsWith(label.replace(/…$/, ""))).toBe(true);
      expect(label).not.toBe(key);
    }
  });

  it("обрезает длинную подпись до MAP_CONFIG.node.labelMaxLength с многоточием", async () => {
    const data = await loadSampleGraphData();
    const longTitle = "А".repeat(MAP_CONFIG.node.labelMaxLength + 10);
    const pub = data.pubs[0];
    if (!pub) throw new Error("фикстура должна содержать хотя бы одну публикацию");
    const pubDetails = new Map([[pub.key, { key: pub.key, label: longTitle } as PubDetail]]);

    const graph = new Graph();
    populateGraph(graph, data, "ru", 3, NO_FILTER, pubDetails);

    const label = graph.getNodeAttribute(pub.key, "label") as string;
    expect(label.length).toBe(MAP_CONFIG.node.labelMaxLength);
    expect(label.endsWith("…")).toBe(true);
  });

  it("отдаёт рёбра только своей вкладки", async () => {
    const data = await loadSampleGraphData();

    const authorsGraph = new Graph();
    populateGraph(authorsGraph, data, "ru", 1, NO_FILTER, NO_PUB_DETAILS);
    expect(realEdgeKeys(authorsGraph)).toHaveLength(data.coauth_edges.length);

    const reposGraph = new Graph();
    populateGraph(reposGraph, data, "ru", 2, NO_FILTER, NO_PUB_DETAILS);
    expect(realEdgeKeys(reposGraph)).toHaveLength(data.repo_edges.length);

    const pubsGraph = new Graph();
    populateGraph(pubsGraph, data, "ru", 3, NO_FILTER, NO_PUB_DETAILS);
    expect(realEdgeKeys(pubsGraph)).toHaveLength(data.pub_edges.length);
  });

  it("filters.minCoauth скрывает слабые связи соавторства на вкладке 1", async () => {
    const data = await loadSampleGraphData();
    const strong = data.coauth_edges.filter((e) => e.w >= 2).length;

    const graph = new Graph();
    populateGraph(graph, data, "ru", 1, { ...NO_FILTER, minCoauth: 2 }, NO_PUB_DETAILS);
    expect(realEdgeKeys(graph)).toHaveLength(strong);
    expect(strong).toBeLessThan(data.coauth_edges.length); // проверка, что фикстура вообще даёт разброс весов
  });

  it("filters.minSharedAuthors скрывает слабые связи публикаций на вкладке 3", async () => {
    const data = await loadSampleGraphData();
    const strong = data.pub_edges.filter((e) => e.w >= 2).length;

    const graph = new Graph();
    populateGraph(graph, data, "ru", 3, { ...NO_FILTER, minSharedAuthors: 2 }, NO_PUB_DETAILS);
    expect(realEdgeKeys(graph)).toHaveLength(strong);
  });

  it("filters.yearMax скрывает публикации позже указанного года, но не публикации без известного года", async () => {
    const data = await loadSampleGraphData();
    const filters = { ...NO_FILTER, yearMax: 2022 };
    const expectedPubs = data.pubs.filter((p) => p.year === null || p.year <= 2022);

    const graph = new Graph();
    populateGraph(graph, data, "ru", 3, filters, NO_PUB_DETAILS);
    expect(realNodeKeys(graph).sort()).toEqual(expectedPubs.map((p) => p.key).sort());
    expect(expectedPubs.some((p) => p.year === null)).toBe(true); // фикстура правда содержит пример без года

    // Ребро между публикациями, у одной из которых год скрыт фильтром, тоже пропадает —
    // graph.hasNode() внутри populateGraph уже отражает фильтр узлов, отдельной
    // проверки года на рёбрах не нужно (в отличие от MapLibre-версии).
    for (const edgeKey of realEdgeKeys(graph)) {
      const [s, t] = graph.extremities(edgeKey);
      expect(realNodeKeys(graph)).toContain(s);
      expect(realNodeKeys(graph)).toContain(t);
    }
  });

  it("filters.showNoDeptAuthors=false скрывает авторов синтетического департамента «Без департамента»", () => {
    const data = dataWithNoDept();

    const shown = new Graph();
    populateGraph(shown, data, "ru", 1, NO_FILTER, NO_PUB_DETAILS);
    const hidden = new Graph();
    populateGraph(hidden, data, "ru", 1, { ...NO_FILTER, showNoDeptAuthors: false }, NO_PUB_DETAILS);

    expect(realNodeKeys(shown)).toEqual(["A1", "A2"]);
    expect(realNodeKeys(hidden)).toEqual(["A1"]); // A2 — из «Без департамента»
  });

  it("filters.showNoDeptPubs=false скрывает публикации синтетического департамента «Без департамента»", () => {
    const data = dataWithNoDept();

    const shown = new Graph();
    populateGraph(shown, data, "ru", 3, NO_FILTER, NO_PUB_DETAILS);
    const hidden = new Graph();
    populateGraph(hidden, data, "ru", 3, { ...NO_FILTER, showNoDeptPubs: false }, NO_PUB_DETAILS);

    expect(realNodeKeys(shown)).toEqual(["P1", "P2"]);
    expect(realNodeKeys(hidden)).toEqual(["P1"]); // P2 — из «Без департамента»
  });
});

describe("deptNodeKey / parseDeptNodeKey", () => {
  it("парсинг возвращает то же число, что было закодировано", () => {
    for (const id of [0, 1, 42]) {
      expect(parseDeptNodeKey(deptNodeKey(id))).toBe(id);
    }
  });

  it("обычный ключ узла — не департамент", () => {
    expect(parseDeptNodeKey("A5133538481")).toBeNull();
    expect(parseDeptNodeKey("example-org/graph-toolkit")).toBeNull();
  });
});

describe("populateGraph — якоря подписей департаментов (не блоб вместо реальных узлов)", () => {
  it("добавляет по якорю на каждый департамент, у которого есть хотя бы один узел в этой вкладке", async () => {
    const data = await loadSampleGraphData();
    const graph = new Graph();
    populateGraph(graph, data, "ru", 1, NO_FILTER, NO_PUB_DETAILS);

    const deptsWithAuthors = new Set(data.authors.map((a) => a.dept));
    for (const deptId of deptsWithAuthors) {
      expect(graph.hasNode(deptNodeKey(deptId))).toBe(true);
    }
  });

  it("якорь невидим (size: 0) — не подменяет реальные узлы блобом", async () => {
    const data = await loadSampleGraphData();
    const graph = new Graph();
    populateGraph(graph, data, "ru", 1, NO_FILTER, NO_PUB_DETAILS);

    const [deptId] = [...new Set(data.authors.map((a) => a.dept))];
    if (deptId === undefined) throw new Error("фикстура должна содержать хотя бы одного автора");
    expect(graph.getNodeAttribute(deptNodeKey(deptId), "size")).toBe(0);
  });

  it("позиция якоря — центроид узлов департамента в этой вкладке", async () => {
    const data = await loadSampleGraphData();
    const graph = new Graph();
    populateGraph(graph, data, "ru", 1, NO_FILTER, NO_PUB_DETAILS);

    const [deptId] = [...new Set(data.authors.map((a) => a.dept))];
    if (deptId === undefined) throw new Error("фикстура должна содержать хотя бы одного автора");
    const deptAuthors = data.authors.filter((a) => a.dept === deptId);

    expect(graph.getNodeAttribute(deptNodeKey(deptId), "x")).toBeCloseTo(
      deptAuthors.reduce((sum, a) => sum + a.gx, 0) / deptAuthors.length,
    );
    expect(graph.getNodeAttribute(deptNodeKey(deptId), "y")).toBeCloseTo(
      deptAuthors.reduce((sum, a) => sum + a.gy, 0) / deptAuthors.length,
    );
  });

  it("рёбра между департаментами берутся из data.dept_edges, а не считаются заново", async () => {
    const data = await loadSampleGraphData();
    const graph = new Graph();
    populateGraph(graph, data, "ru", 1, NO_FILTER, NO_PUB_DETAILS);

    for (const edge of data.dept_edges) {
      if (graph.hasNode(deptNodeKey(edge.s)) && graph.hasNode(deptNodeKey(edge.t))) {
        expect(graph.hasEdge(deptNodeKey(edge.s), deptNodeKey(edge.t))).toBe(true);
      }
    }
  });
});

describe("applyGraphStyling (через mountReactiveGraph) — подписи департаментов vs узлов по зуму", () => {
  it("далеко (ratio выше порога): якорь департамента получает forceLabel, обычный узел теряет подпись", async () => {
    const data = await loadSampleGraphData();
    const graph = new Graph();
    graph.addNode("A1", { x: 0, y: 0 });
    graph.addNode(deptNodeKey(0), { x: 0, y: 0, size: 0 });
    const store = new Store<AppState>(initialState());
    const { renderer, getReducer, fireCameraUpdated } = fakeRenderer(graph);

    mountReactiveGraph(renderer, store, data, NO_PUB_DETAILS);
    const nodeReducer = getReducer("nodeReducer");
    fireCameraUpdated(MAP_CONFIG.region.ratioThreshold + 1);

    expect(nodeReducer(deptNodeKey(0), NODE_BASE)).toMatchObject({ forceLabel: true });
    expect(nodeReducer("A1", NODE_BASE)).toMatchObject({ label: "" });
  });

  it("близко (ratio ниже порога): обычный узел сохраняет подпись, якорь департамента её не форсирует", async () => {
    const data = await loadSampleGraphData();
    const graph = new Graph();
    graph.addNode("A1", { x: 0, y: 0 });
    graph.addNode(deptNodeKey(0), { x: 0, y: 0, size: 0 });
    const store = new Store<AppState>(initialState());
    const { renderer, getReducer, fireCameraUpdated } = fakeRenderer(graph);

    mountReactiveGraph(renderer, store, data, NO_PUB_DETAILS);
    const nodeReducer = getReducer("nodeReducer");
    fireCameraUpdated(MAP_CONFIG.region.ratioThreshold - 1);

    expect(nodeReducer("A1", NODE_BASE)).toMatchObject({ label: NODE_BASE.label });
    expect(nodeReducer(deptNodeKey(0), NODE_BASE)).toMatchObject({ forceLabel: false });
  });

  it("выбранный обычный узел сохраняет подпись, даже когда показываются подписи департаментов", async () => {
    const data = await loadSampleGraphData();
    const graph = new Graph();
    graph.addNode("A1", { x: 0, y: 0 });
    const store = new Store<AppState>(initialState({ selection: { kind: "node", key: "A1" } }));
    const { renderer, getReducer, fireCameraUpdated } = fakeRenderer(graph);

    mountReactiveGraph(renderer, store, data, NO_PUB_DETAILS);
    fireCameraUpdated(MAP_CONFIG.region.ratioThreshold + 1);

    expect(getReducer("nodeReducer")("A1", NODE_BASE)).toMatchObject({ label: NODE_BASE.label, highlighted: true });
  });
});

describe("applyGraphStyling (через mountReactiveGraph) — выбор/наведение и притухание соседей", () => {
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
    // Сосед — цвет/подпись не тронуты, но крупнее обычного (третий, промежуточный уровень яркости).
    expect(nodeReducer("A2", NODE_BASE)).toMatchObject({
      color: NODE_BASE.color,
      label: NODE_BASE.label,
      size: MAP_CONFIG.node.radius * MAP_CONFIG.node.neighborSizeScale,
    });
    expect(nodeReducer("A3", NODE_BASE)).toMatchObject({ color: MAP_CONFIG.node.dimColor, label: "" }); // не сосед — притушен
  });

  it("selection на ключ, которого нет в ТЕКУЩЕМ графе (например, после смены вкладки), не роняет reducer", async () => {
    // Регрессия: раньше nodeReducer вызывал graph.areNeighbors(focus, ...)
    // без проверки, что focus вообще существует в графе — graphology
    // бросает исключение на несуществующем узле, а не возвращает false.
    // Реалистичный сценарий — клик по узлу одной вкладки, затем переход на
    // другую: граф пересобирается под новый набор сущностей, а выбор мог
    // (по ошибке в другом месте, или до фикса в features/tabs/index.ts)
    // пережить смену вкладки. Проверяем сам reducer — последний рубеж
    // защиты, а не только то, что вызывающий код теперь сбрасывает выбор.
    const data = await loadSampleGraphData();
    const graph = new Graph();
    graph.addNode("A2", { x: 1, y: 1 });
    const store = new Store<AppState>(initialState({ selection: { kind: "node", key: "A1" } })); // "A1" отсутствует в graph
    const { renderer, getReducer } = fakeRenderer(graph);

    mountReactiveGraph(renderer, store, data, NO_PUB_DETAILS);

    expect(() => getReducer("nodeReducer")("A2", NODE_BASE)).not.toThrow();
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
    // Сосед наведённого A2 — не притушен, но и НЕ увеличен (в отличие от
    // соседа ВЫБРАННОГО узла): рост размера на hover в плотных скоплениях
    // сдвигает хитбокс под курсор и ломает наведение — прямая жалоба
    // пользователя ("наведение на кучу — рандом какой-то").
    expect(nodeReducer("A3", NODE_BASE)).toMatchObject({ color: NODE_BASE.color, size: NODE_BASE.size });
    expect(nodeReducer("A1", NODE_BASE)).toMatchObject({ color: MAP_CONFIG.node.dimColor, label: "" }); // не сосед — притушен

    fire("leaveNode", {});
    expect(nodeReducer("A1", NODE_BASE)).toEqual(NODE_BASE); // наведение снято — фокуса больше нет
  });

  it("выбор департамента фокусирует так же, как выбор узла — соседние департаменты не притушены", async () => {
    const data = await loadSampleGraphData();
    const graph = new Graph();
    graph.addNode(deptNodeKey(0), { x: 0, y: 0, size: 0 });
    graph.addNode(deptNodeKey(1), { x: 1, y: 1, size: 0 });
    graph.addNode(deptNodeKey(2), { x: 2, y: 2, size: 0 });
    graph.addEdge(deptNodeKey(0), deptNodeKey(1));
    const store = new Store<AppState>(initialState({ selection: { kind: "dept", id: 0 } }));
    const { renderer, getReducer } = fakeRenderer(graph);

    mountReactiveGraph(renderer, store, data, NO_PUB_DETAILS);
    const nodeReducer = getReducer("nodeReducer");

    expect(nodeReducer(deptNodeKey(0), NODE_BASE)).toMatchObject({ highlighted: true });
    // Сосед-департамент — не притушен, но и не увеличен (isRegion исключён из
    // neighborSizeScale-ветки): фиксированный размер сделал бы невидимую
    // (size: 0) точку-якорь видимым кружком там, где его никогда не было.
    expect(nodeReducer(deptNodeKey(1), NODE_BASE)).toMatchObject({ color: NODE_BASE.color, size: NODE_BASE.size });
    expect(nodeReducer(deptNodeKey(2), NODE_BASE)).toMatchObject({ color: MAP_CONFIG.node.dimColor }); // не сосед — притушен
  });

  it("edgeReducer скрывает рёбра, не задевающие фокус, но не трогает задевающие", async () => {
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
    expect(edgeReducer(notTouchingFocus, EDGE_BASE)).toMatchObject({ hidden: true }); // "остальные рёбра убрать", не притушить
  });

  it("edgeReducer подсвечивает выбранное ребро независимо от порядка s/t", async () => {
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

  it("рёбра между департаментами никогда не рисуются линией", async () => {
    const data = await loadSampleGraphData();
    const graph = new Graph();
    graph.addNode(deptNodeKey(0), { x: 0, y: 0, size: 0 });
    graph.addNode(deptNodeKey(1), { x: 1, y: 1, size: 0 });
    graph.addEdge(deptNodeKey(0), deptNodeKey(1), { weight: 2 });
    const store = new Store<AppState>(initialState());
    const { renderer, getReducer, fireCameraUpdated } = fakeRenderer(graph);

    mountReactiveGraph(renderer, store, data, NO_PUB_DETAILS);
    fireCameraUpdated(MAP_CONFIG.edge.visibleBelowRatio - 1); // на этом ratio реальные рёбра были бы видны
    const [edgeKey] = graph.edges();
    if (!edgeKey) throw new Error("граф должен содержать ребро");

    expect(getReducer("edgeReducer")(edgeKey, EDGE_BASE)).toMatchObject({ hidden: true });
  });
});

describe("applyGraphStyling — видимость рёбер по camera.ratio", () => {
  it("прячет рёбра, когда ratio выше порога, показывает — когда ниже", async () => {
    const data = await loadSampleGraphData();
    const graph = new Graph();
    graph.addNode("A1", { x: 0, y: 0 });
    graph.addNode("A2", { x: 1, y: 1 });
    graph.addEdge("A1", "A2");
    const store = new Store<AppState>(initialState());
    const { renderer, getReducer, fireCameraUpdated } = fakeRenderer(graph);

    mountReactiveGraph(renderer, store, data, NO_PUB_DETAILS);
    const edgeReducer = getReducer("edgeReducer");
    const [edgeKey] = graph.edges();
    if (!edgeKey) throw new Error("граф должен содержать ребро");

    fireCameraUpdated(MAP_CONFIG.edge.visibleBelowRatio + 1);
    expect(edgeReducer(edgeKey, EDGE_BASE)).toMatchObject({ hidden: true });

    fireCameraUpdated(MAP_CONFIG.edge.visibleBelowRatio - 1);
    expect(edgeReducer(edgeKey, EDGE_BASE)).not.toMatchObject({ hidden: true });
  });
});

describe("mountZoomDebug", () => {
  it("рисует индикатор ВНУТРИ контейнера карты (не document.body), показывает camera.ratio, обновляется, unmount убирает элемент", () => {
    let ratioHandler: ((state: { ratio: number }) => void) | undefined;
    let ratio = 1.234;
    const container = document.createElement("div");
    document.body.appendChild(container);
    const renderer = {
      getContainer: () => container,
      getCamera: () => ({
        getState: () => ({ ratio, x: 0, y: 0, angle: 0 }),
        on: (event: string, cb: (state: { ratio: number }) => void) => {
          if (event === "updated") ratioHandler = cb;
        },
        off: vi.fn(),
      }),
    } as unknown as Sigma;

    const unmount = mountZoomDebug(renderer);
    const el = container.lastElementChild as HTMLElement;
    expect(el.textContent).toContain("1.234");

    ratio = 5.678;
    ratioHandler?.({ ratio });
    expect(el.textContent).toContain("5.678");

    unmount();
    expect(container.contains(el)).toBe(false);
  });
});

describe("mountReactiveGraph", () => {
  it("не пересобирает граф при монтировании (он уже наполнен снаружи) и пересобирает при смене tab/lang/filters, но не при смене selection", async () => {
    const data = await loadSampleGraphData();
    const graph = new Graph();
    populateGraph(graph, data, "ru", 1, NO_FILTER, NO_PUB_DETAILS);
    const store = new Store<AppState>(initialState());
    const { renderer, refresh } = fakeRenderer(graph);
    const authorsRealCount = realNodeKeys(graph).length;

    mountReactiveGraph(renderer, store, data, NO_PUB_DETAILS);
    expect(realNodeKeys(graph)).toHaveLength(authorsRealCount);

    store.set({ selection: { kind: "node", key: "A1" } });
    expect(refresh).toHaveBeenCalledTimes(1); // выбор — лёгкий refresh(), не пересборка
    expect(realNodeKeys(graph)).toHaveLength(authorsRealCount); // граф не пересобирался

    store.set({ tab: 2 });
    expect(realNodeKeys(graph)).toHaveLength(data.repos.length); // пересобран под новую вкладку
    // "A1" (автор) не существует в новом графе (репозитории) — mountReactiveGraph
    // сама обнуляет устаревший выбор (см. отдельный тест ниже), это и даёт
    // второй refresh() здесь, не сама пересборка.
    expect(store.get().selection).toBeNull();
    expect(refresh).toHaveBeenCalledTimes(2);

    store.set({ lang: "en" });
    expect(realNodeKeys(graph)).toHaveLength(data.repos.length); // та же вкладка, граф пересобран заново (не упал)

    store.set({ filters: { ...store.get().filters, minCoauth: 5 } });
    expect(refresh).toHaveBeenCalledTimes(2); // ни одно из трёх пересобраний само по себе refresh() не дёргало
  });

  it("смена фильтра, скрывающего выбранную публикацию (filters.yearMax), тоже обнуляет устаревший выбор", async () => {
    const data = await loadSampleGraphData();
    const graph = new Graph();
    populateGraph(graph, data, "ru", 3, NO_FILTER, NO_PUB_DETAILS);
    const pub2024 = data.pubs.find((p) => p.year === 2024);
    if (!pub2024) throw new Error("фикстура должна содержать публикацию 2024 года");
    const store = new Store<AppState>(initialState({ tab: 3, selection: { kind: "node", key: pub2024.key } }));
    const { renderer } = fakeRenderer(graph);

    mountReactiveGraph(renderer, store, data, NO_PUB_DETAILS);
    store.set({ filters: { ...store.get().filters, yearMax: 2022 } }); // скрывает pub2024 (год > 2022)

    expect(graph.hasNode(pub2024.key)).toBe(false); // публикация правда пропала из графа
    expect(store.get().selection).toBeNull();
  });

  it("выбор узла подлетает камерой к координатам из getNodeDisplayData (framed graph), а НЕ к сырым graph.getNodeAttributes()", async () => {
    const data = await loadSampleGraphData();
    const graph = new Graph();
    graph.addNode("A1", { x: 12, y: 34 });
    const store = new Store<AppState>(initialState());
    const { renderer, cameraAnimate } = fakeRenderer(graph);

    mountReactiveGraph(renderer, store, data, NO_PUB_DETAILS);
    store.set({ selection: { kind: "node", key: "A1" } });

    // x/y сдвинуты на FRAMED_COORD_OFFSET относительно сырых атрибутов узла
    // (см. fakeRenderer::getNodeDisplayData) — так тест ловит регрессию,
    // из-за которой камера раньше улетала в пустоту на реальных данных:
    // graph.getNodeAttributes() отдаёт СЫРЫЕ координаты ForceAtlas2, а не то
    // нормализованное пространство, в котором живёт camera.x/y.
    expect(cameraAnimate).toHaveBeenCalledWith(
      { x: 12 + FRAMED_COORD_OFFSET, y: 34 + FRAMED_COORD_OFFSET, ratio: MAP_CONFIG.camera.focusRatio },
      { duration: MAP_CONFIG.camera.focusDuration, easing: "quadraticInOut" },
    );
  });

  it("выбор департамента подлетает камерой к координатам его якоря из getNodeDisplayData", async () => {
    const data = await loadSampleGraphData();
    const graph = new Graph();
    graph.addNode(deptNodeKey(0), { x: 5, y: 6, size: 0 });
    const store = new Store<AppState>(initialState());
    const { renderer, cameraAnimate } = fakeRenderer(graph);

    mountReactiveGraph(renderer, store, data, NO_PUB_DETAILS);
    store.set({ selection: { kind: "dept", id: 0 } });

    expect(cameraAnimate).toHaveBeenCalledWith(
      { x: 5 + FRAMED_COORD_OFFSET, y: 6 + FRAMED_COORD_OFFSET, ratio: MAP_CONFIG.camera.focusRatio },
      { duration: MAP_CONFIG.camera.focusDuration, easing: "quadraticInOut" },
    );
  });

  it("выбор ребра камеру не двигает — у ребра нет одной точки для подлёта", async () => {
    const data = await loadSampleGraphData();
    const graph = new Graph();
    graph.addNode("A1", { x: 0, y: 0 });
    graph.addNode("A2", { x: 1, y: 1 });
    graph.addEdge("A1", "A2", { weight: 1 });
    const store = new Store<AppState>(initialState());
    const { renderer, cameraAnimate } = fakeRenderer(graph);

    mountReactiveGraph(renderer, store, data, NO_PUB_DETAILS);
    store.set({ selection: { kind: "edge", s: "A1", t: "A2", w: 1 } });

    expect(cameraAnimate).not.toHaveBeenCalled();
  });

  it("выбор ребра переживает пересборку графа (смена lang) независимо от порядка s/t — graph.hasEdge() чувствителен к направлению, ребро выбора — нет", async () => {
    // populateGraph добавляет рёбра через generic graph.mergeEdge(), которое
    // на графе типа "mixed" (дефолт graphology) создаёт НАПРАВЛЕННОЕ ребро
    // s -> t — graph.hasEdge(t, s) для него вернуло бы false. Рёбра в этом
    // приложении везде считаются неориентированными (core/url.ts и
    // applyGraphStyling::isSelectedEdge уже проверяют оба порядка) —
    // selectionExistsIn() должна делать то же самое.
    const data = await loadSampleGraphData();
    const graph = new Graph();
    populateGraph(graph, data, "ru", 1, NO_FILTER, NO_PUB_DETAILS);
    const edge = data.coauth_edges[0];
    if (!edge) throw new Error("фикстура должна содержать хотя бы одно coauth-ребро");
    const store = new Store<AppState>(
      initialState({ selection: { kind: "edge", s: edge.t, t: edge.s, w: edge.w } }), // s/t НАРОЧНО перевёрнуты
    );
    const { renderer } = fakeRenderer(graph);

    mountReactiveGraph(renderer, store, data, NO_PUB_DETAILS);
    store.set({ lang: "en" }); // безобидная пересборка, но всё равно проходит через selectionExistsIn()

    expect(store.get().selection).not.toBeNull();
  });
});
