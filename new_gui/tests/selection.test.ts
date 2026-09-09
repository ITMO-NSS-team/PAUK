import Graph from "graphology";
import type Sigma from "sigma";
import { describe, expect, it, vi } from "vitest";
import { Store, type AppState } from "../src/core/state";
import { mountSelection } from "../src/features/selection";

function initialState(): AppState {
  return {
    tab: 1,
    lang: "ru",
    selection: null,
    filters: { minCoauth: 1, minSharedAuthors: 1, yearMax: 2026 },
  };
}

/**
 * Фейковый Sigma-рендерер: хранит реальный graphology.Graph (нужен
 * mountSelection для extremities()/getEdgeAttribute() при клике по ребру)
 * и перехватывает on(event, cb) по имени события — тест вызывает
 * сохранённый колбэк напрямую вместо настоящего клика мышью. Настоящий
 * Sigma в jsdom не поднять (нужен WebGL-канвас).
 */
function fakeRenderer(graph: Graph): { renderer: Sigma; fire: (event: string, payload?: unknown) => void } {
  const handlers = new Map<string, (payload?: unknown) => void>();
  const renderer = {
    getGraph: () => graph,
    getContainer: () => ({ style: {} }) as unknown as HTMLElement,
    on: (event: string, cb: (payload?: unknown) => void) => handlers.set(event, cb),
    off: vi.fn(),
  } as unknown as Sigma;
  return { renderer, fire: (event, payload) => handlers.get(event)?.(payload) };
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
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "node", key: "A1" } });
    const { renderer, fire } = fakeRenderer(graph);

    mountSelection(renderer, store);
    fire("clickStage");

    expect(store.get().selection).toBeNull();
  });
});
