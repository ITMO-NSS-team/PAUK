import Graph from "graphology";
import type Sigma from "sigma";
import { describe, expect, it, vi } from "vitest";
import { Store, type AppState } from "../src/core/state";
import { mountSelection } from "../src/features/selection";

function initialState(): AppState {
  return {
    screen: "app",
    tab: 1,
    lang: "ru",
    selection: null,
    filters: {
      minCoauth: 1,
      minSharedAuthors: 1,
      yearMax: 2026,
      showNoDeptAuthors: true,
      showNoDeptPubs: true,
      showExternalAuthors: false,
      showIsolatedAuthors: true,
      edgeZoomThreshold: 0.4,
      showRegions: { 1: false, 2: false, 3: false },
      regionZoomThreshold: 0.25,
      regionMinNodes: 10,
    },
  };
}

/**
 * Фейковый Sigma-рендерер: хранит реальный graphology.Graph (нужен
 * mountSelection для extremities()/getEdgeAttribute() при клике по ребру)
 * и перехватывает on(event, cb) по имени события — тест вызывает
 * сохранённый колбэк напрямую вместо настоящего клика мышью. Настоящий
 * Sigma в jsdom не поднять (нужен WebGL-канвас).
 */
function fakeRenderer(
  graph: Graph,
  ratio = 1,
): {
  renderer: Sigma;
  container: { style: { cursor?: string } };
  fire: (event: string, payload?: unknown) => void;
} {
  const handlers = new Map<string, (payload?: unknown) => void>();
  const container: { style: { cursor?: string } } = { style: {} };
  const renderer = {
    getGraph: () => graph,
    getContainer: () => container as unknown as HTMLElement,
    getCamera: () => ({ getState: () => ({ ratio }) }),
    on: (event: string, cb: (payload?: unknown) => void) => handlers.set(event, cb),
    off: vi.fn(),
  } as unknown as Sigma;
  return { renderer, container, fire: (event, payload) => handlers.get(event)?.(payload) };
}

describe("mountSelection", () => {
  it("клик по узлу выбирает узел", () => {
    const graph = new Graph();
    const store = new Store<AppState>(initialState());
    const { renderer, fire } = fakeRenderer(graph);

    mountSelection(renderer, store);
    fire("clickNode", { node: "A1" });

    expect(store.get().selection).toEqual({ kind: "node", key: "A1" });
  });

  it("клик по ребру выбирает ребро — s/t/w читаются из графа по ключу ребра", () => {
    const graph = new Graph();
    graph.addNode("A1", { x: 0, y: 0 });
    graph.addNode("A2", { x: 1, y: 1 });
    graph.addEdge("A1", "A2", { weight: 3 });
    const store = new Store<AppState>(initialState());
    const { renderer, fire } = fakeRenderer(graph);

    mountSelection(renderer, store);
    fire("clickEdge", { edge: graph.edges()[0] });

    expect(store.get().selection).toEqual({ kind: "edge", s: "A1", t: "A2", w: 3 });
  });

  it("клик по пустому месту (clickStage) снимает выбор", () => {
    const graph = new Graph();
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: "A1" },
    });
    const { renderer, fire } = fakeRenderer(graph);

    mountSelection(renderer, store);
    fire("clickStage", { event: { x: 10, y: 20 } });

    expect(store.get().selection).toBeNull();
  });

  it("клик по пустому месту внутри видимого региона выбирает его департамент", () => {
    const graph = new Graph();
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: "A1" },
    });
    const { renderer, fire } = fakeRenderer(graph);
    const deptAtViewport = vi.fn((point: { x: number; y: number }) => (point.x < 50 ? 7 : null));

    mountSelection(renderer, store, deptAtViewport);

    fire("clickStage", { event: { x: 10, y: 20 } });
    expect(deptAtViewport).toHaveBeenCalledWith({ x: 10, y: 20 });
    expect(store.get().selection).toEqual({ kind: "dept", id: 7 });

    fire("clickStage", { event: { x: 90, y: 20 } }); // вне регионов — выбор снимается, как раньше
    expect(store.get().selection).toBeNull();
  });
  it("в режиме регионов (регионы включены, камера дальше порога) клик по узлу или ребру выбирает регион под курсором, а не узел", () => {
    const graph = new Graph();
    graph.addNode("A1");
    graph.addNode("A2");
    graph.addEdge("A1", "A2", { weight: 1 });
    const state = initialState();
    const store = new Store<AppState>({
      ...state,
      filters: { ...state.filters, showRegions: { 1: true, 2: false, 3: true } },
    });
    const { renderer, container, fire } = fakeRenderer(graph, 1); // ratio 1 > regionZoomThreshold 0.25

    mountSelection(renderer, store, () => 4);

    fire("clickNode", { node: "A1", event: { x: 1, y: 1 } });
    expect(store.get().selection).toEqual({ kind: "dept", id: 4 });

    fire("clickEdge", { edge: graph.edges()[0], event: { x: 1, y: 1 } });
    expect(store.get().selection).toEqual({ kind: "dept", id: 4 });

    fire("enterNode", { node: "A1" }); // курсором в режиме регионов управляют регионы
    expect(container.style.cursor).toBeUndefined();
  });

  it("ближе порога (режим узлов) клик по узлу выбирает узел, даже если регионы включены", () => {
    const graph = new Graph();
    graph.addNode("A1");
    const state = initialState();
    const store = new Store<AppState>({
      ...state,
      filters: { ...state.filters, showRegions: { 1: true, 2: false, 3: true } },
    });
    const { renderer, fire } = fakeRenderer(graph, 0.1);

    mountSelection(renderer, store, () => 4);
    fire("clickNode", { node: "A1", event: { x: 1, y: 1 } });

    expect(store.get().selection).toEqual({ kind: "node", key: "A1" });
  });
});
