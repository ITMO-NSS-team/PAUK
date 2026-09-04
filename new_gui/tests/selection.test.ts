import type { GeoJSONFeature, Map as MapLibreMap, MapMouseEvent } from "maplibre-gl";
import { describe, expect, it, vi } from "vitest";
import { Store, type AppState } from "../src/core/state";
import { mountSelection } from "../src/features/selection";
import { EDGE_LAYER_ID, NODE_LAYER_ID } from "../src/map/build";

function initialState(): AppState {
  return {
    tab: 1,
    lang: "ru",
    selection: null,
    filters: { minCoauth: 1, minPubAuthors: 1, yearMax: 2026 },
  };
}

/**
 * Фейковая карта: queryRenderedFeatures отдаёт то, что попросили через
 * withFeatures — по слою из opts.layers[0], как реально их запрашивает
 * features/selection.ts (по одному слою за раз). onClick сохраняется,
 * чтобы тест мог вызвать его напрямую вместо настоящего клика мышью.
 */
function fakeMap(byLayer: Record<string, GeoJSONFeature | undefined>): { map: MapLibreMap; click: () => void } {
  let onClick: ((event: MapMouseEvent) => void) | undefined;
  const map = {
    queryRenderedFeatures: (_point: unknown, opts: { layers: string[] }) => {
      const feature = byLayer[opts.layers[0] ?? ""];
      return feature ? [feature] : [];
    },
    getCanvas: () => ({ style: {} }) as unknown as HTMLCanvasElement,
    on: (event: string, arg2: unknown, arg3?: unknown) => {
      if (event !== "click") return;
      onClick = (typeof arg3 === "function" ? arg3 : arg2) as (event: MapMouseEvent) => void;
    },
    off: vi.fn(),
    // mountSelection тоже подписывается на store и красит выбранный узел —
    // без этой заглушки клик падал бы на "setPaintProperty is not a function".
    setPaintProperty: vi.fn(),
  } as unknown as MapLibreMap;

  return { map, click: () => onClick?.({} as MapMouseEvent) };
}

function nodeFeature(key: string): GeoJSONFeature {
  return { properties: { key } } as unknown as GeoJSONFeature;
}

function edgeFeature(s: string, t: string, w: number): GeoJSONFeature {
  return { properties: { s, t, w } } as unknown as GeoJSONFeature;
}

describe("mountSelection", () => {
  it("клик по узлу выбирает узел", () => {
    const store = new Store<AppState>(initialState());
    const { map, click } = fakeMap({ [NODE_LAYER_ID]: nodeFeature("A1") });

    mountSelection(map, store);
    click();

    expect(store.get().selection).toEqual({ kind: "node", key: "A1" });
  });

  it("клик по ребру (без узла под курсором) выбирает ребро", () => {
    const store = new Store<AppState>(initialState());
    const { map, click } = fakeMap({ [EDGE_LAYER_ID]: edgeFeature("A1", "A2", 3) });

    mountSelection(map, store);
    click();

    expect(store.get().selection).toEqual({ kind: "edge", s: "A1", t: "A2", w: 3 });
  });

  it("узел приоритетнее ребра, если под курсором оба", () => {
    const store = new Store<AppState>(initialState());
    const { map, click } = fakeMap({
      [NODE_LAYER_ID]: nodeFeature("A1"),
      [EDGE_LAYER_ID]: edgeFeature("A1", "A2", 3),
    });

    mountSelection(map, store);
    click();

    expect(store.get().selection).toEqual({ kind: "node", key: "A1" });
  });

  it("клик по пустому месту снимает выбор", () => {
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "node", key: "A1" } });
    const { map, click } = fakeMap({});

    mountSelection(map, store);
    click();

    expect(store.get().selection).toBeNull();
  });
});
