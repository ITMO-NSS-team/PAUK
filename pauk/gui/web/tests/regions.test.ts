import type Sigma from "sigma";
import { describe, expect, it, vi } from "vitest";
import type { AuthorNode, Department, GraphData } from "../src/contracts/graph";
import { NO_DEPT_COLOR, REGION_CONFIG } from "../src/core/config";
import { Store, type AppState } from "../src/core/state";
import {
  buildRegions,
  mountRegions,
  regionDeptAt,
  wrapLabel,
  type RegionPoint,
} from "../src/map/regions";

function cluster(cx: number, cy: number, count: number, dept: number, step = 2): RegionPoint[] {
  const side = Math.ceil(Math.sqrt(count));
  return Array.from({ length: count }, (_, i) => ({
    x: cx + (i % side) * step,
    y: cy + Math.floor(i / side) * step,
    dept,
  }));
}

describe("buildRegions", () => {
  it("two distant clusters of different departments give one region each, and each covers its own nodes", () => {
    const a = cluster(0, 0, 16, 1);
    const b = cluster(200, 0, 16, 2);

    const regions = buildRegions([...a, ...b], 10);

    expect(regions.map((r) => r.dept)).toEqual([1, 2]);
    for (const p of a) expect(regionDeptAt(regions, p)).toBe(1);
    for (const p of b) expect(regionDeptAt(regions, p)).toBe(2);
    expect(regionDeptAt(regions, { x: 100, y: 0 })).toBeNull(); // the gap between clusters stays empty
  });

  it("neighbouring departments do not overlap: a point does not lie in two regions at once", () => {
    const regions = buildRegions([...cluster(0, 0, 25, 1), ...cluster(12, 0, 25, 2)], 10);

    let checked = 0;
    for (let x = -10; x <= 30; x += 0.5) {
      for (let y = -10; y <= 20; y += 0.5) {
        const inside = regions.filter((region) => regionDeptAt([region], { x, y }) !== null);
        expect(inside.length).toBeLessThanOrEqual(1);
        checked++;
      }
    }
    expect(checked).toBeGreaterThan(0);
    expect(regions.map((r) => r.dept)).toEqual([1, 2]);
  });

  it("an island smaller than minNodes is dropped, while a large island of the same department stays", () => {
    const big = cluster(0, 0, 20, 1);
    const small = cluster(300, 0, 4, 1);

    const regions = buildRegions([...big, ...small], 10);

    expect(regions).toHaveLength(1);
    expect(regionDeptAt(regions, big[0] ?? { x: 0, y: 0 })).toBe(1);
    expect(regionDeptAt(regions, small[0] ?? { x: 0, y: 0 })).toBeNull();
  });

  it("several large islands of one department are all drawn, not only the biggest one", () => {
    const regions = buildRegions([...cluster(0, 0, 20, 1), ...cluster(300, 0, 12, 1)], 10);

    expect(regions).toHaveLength(1);
    expect(regionDeptAt(regions, { x: 2, y: 2 })).toBe(1);
    expect(regionDeptAt(regions, { x: 302, y: 2 })).toBe(1);
    expect(regions[0]?.rings.length).toBeGreaterThanOrEqual(2);
  });

  it("the label point is the centre of the nodes of the largest island, not of all department nodes", () => {
    const big = cluster(0, 0, 20, 1); // 4 rows of 5: x 0..8, y 0..6 -> center (4, 3)
    const regions = buildRegions([...big, ...cluster(300, 0, 12, 1)], 10);

    expect(regions[0]?.label.x).toBeCloseTo(4);
    expect(regions[0]?.label.y).toBeCloseTo(3);
  });

  it("a department with fewer than minNodes nodes overall is not in the result; empty input gives an empty result", () => {
    expect(buildRegions(cluster(0, 0, 5, 1), 10)).toEqual([]);
    expect(buildRegions([], 10)).toEqual([]);
  });
});

function authorNode(key: string, point: RegionPoint): AuthorNode {
  return {
    key,
    kind: "author",
    dept: point.dept,
    label: key,
    label_en: key,
    pubs_count: 1,
    rank: 1,
    gx: point.x,
    gy: point.y,
  };
}

function department(id: number, name: string, color: string): Department {
  return { id, name, name_en: name, color, n: 16, n_authors: 16, n_pubs: 0, n_repos: 0 };
}

/** Department 0 clusters at (0, 0); "no department" (9) at (200, 0). */
function sampleData(): GraphData {
  return {
    departments: [
      department(0, "Кафедра", "#ff0000"),
      department(9, "Без департамента", NO_DEPT_COLOR),
    ],
    dept_edges: [],
    authors: [
      ...cluster(0, 0, 16, 0).map((p, i) => authorNode(`A${i}`, p)),
      ...cluster(200, 0, 16, 9).map((p, i) => authorNode(`N${i}`, p)),
    ],
    coauth_edges: [],
    repos: [],
    repo_edges: [],
    repo_author_edges: [],
    repo_pub_edges: [],
    pubs: [],
    pub_edges: [],
    all_edges: [],
  };
}

function initialState(overrides: Partial<AppState["filters"]> = {}): AppState {
  return {
    screen: "app",
    tab: 1,
    lang: "ru",
    theme: "dark",
    selection: null,
    filters: {
      minCoauth: 1,
      minSharedAuthors: 1,
      yearMax: 2026,
      showNoDeptAuthors: true,
      showNoDeptPubs: true,
      showExternalAuthors: false,
      showIsolatedAuthors: true,
      edgeZoomThreshold: 0.25,
      showRegions: { 1: true, 2: false, 3: true },
      regionZoomThreshold: 0.25,
      regionMinNodes: 10,
      ...overrides,
    },
  };
}

/** Canvas with a stubbed 2d context (jsdom does not draw); layout coords equal screen coords. */
function fakeRenderer(ratio: { value: number }) {
  const context = {
    setTransform: vi.fn(),
    clearRect: vi.fn(),
    beginPath: vi.fn(),
    moveTo: vi.fn(),
    lineTo: vi.fn(),
    closePath: vi.fn(),
    fill: vi.fn(),
    stroke: vi.fn(),
    fillText: vi.fn(),
    strokeText: vi.fn(),
    measureText: (text: string) => ({ width: text.length * 7 }),
    font: "",
    textAlign: "",
    textBaseline: "",
    lineJoin: "",
    fillStyle: "",
    strokeStyle: "",
    globalAlpha: 1,
    lineWidth: 1,
  };
  // Both canvases (fill and labels) share one fake context.
  const createCanvas = vi.fn(() => {
    const canvas = document.createElement("canvas");
    canvas.getContext = (() => context) as unknown as HTMLCanvasElement["getContext"];
    return canvas;
  });
  const handlers = new Map<string, () => void>();
  const captorHandlers = new Map<string, (event: { x: number; y: number }) => void>();
  const container = { style: { cursor: "" } };
  const renderer = {
    getMouseCaptor: () => ({
      on: (event: string, cb: (e: { x: number; y: number }) => void) =>
        captorHandlers.set(event, cb),
      off: vi.fn(),
    }),
    getContainer: () => container,
    createCanvas,
    getSetting: () => "Arial",
    getDimensions: () => ({ width: 100, height: 100 }),
    getCamera: () => ({ getState: () => ({ ratio: ratio.value }) }),
    graphToViewport: (p: { x: number; y: number }) => p,
    viewportToGraph: (p: { x: number; y: number }) => p,
    scheduleRender: vi.fn(),
    on: (event: string, cb: () => void) => handlers.set(event, cb),
    off: vi.fn(),
  } as unknown as Sigma;
  return {
    renderer,
    context,
    container,
    render: () => handlers.get("afterRender")?.(),
    moveMouse: (x: number, y: number) => captorHandlers.get("mousemove")?.({ x, y }),
  };
}

describe("wrapLabel", () => {
  const measure = (line: string) => line.length * 10;

  it("wraps by words without exceeding the line width", () => {
    expect(wrapLabel("Институт прикладных компьютерных наук", measure, 200, 3)).toEqual([
      "Институт прикладных",
      "компьютерных наук",
    ]);
  });

  it("no more than maxLines lines, the last one is truncated with an ellipsis and also fits the width", () => {
    const lines = wrapLabel("один два три четыре пять шесть семь", measure, 80, 2);
    expect(lines).toHaveLength(2);
    expect(lines[1]?.endsWith("…")).toBe(true);
    expect(measure(lines[1] ?? "")).toBeLessThanOrEqual(80);
  });

  it("a word longer than the width stays whole", () => {
    expect(wrapLabel("Сверхдлинноеслово", measure, 50, 3)).toEqual(["Сверхдлинноеслово"]);
  });
});

describe("mountRegions", () => {
  it("puts its own canvas under the edges and draws regions while the camera is farther than the threshold", () => {
    const ratio = { value: 1 };
    const { renderer, context, render } = fakeRenderer(ratio);
    mountRegions(renderer, new Store<AppState>(initialState()), sampleData());

    expect(renderer.createCanvas).toHaveBeenCalledWith("regions", { beforeLayer: "edges" });
    expect(renderer.createCanvas).toHaveBeenCalledWith("region-labels", { afterLayer: "labels" });
    render();
    // Label at the island center (4x4 grid, step 2, at (0, 0) -> (3, 3)), in the current language.
    expect(context.fillText).toHaveBeenCalledWith("Кафедра", 3, 3);
    expect(context.fill).toHaveBeenCalledWith("evenodd");
    expect(context.fillStyle).toBe("#ff0000"); // real department only, not "no department"
    expect(context.fill).toHaveBeenCalledTimes(1);

    context.fill.mockClear();
    context.fillText.mockClear();
    context.stroke.mockClear();
    ratio.value = 0.2; // below the 0.25 threshold: stroke only
    render();
    expect(context.clearRect).toHaveBeenCalled();
    expect(context.stroke).toHaveBeenCalled();
    expect(context.fill).not.toHaveBeenCalled();
    expect(context.fillText).not.toHaveBeenCalled();
  });

  it("does not draw on a tab where regions are off, and recomputes when filters change", () => {
    const { renderer, context, render } = fakeRenderer({ value: 1 });
    const store = new Store<AppState>(
      initialState({ showRegions: { 1: false, 2: false, 3: true } }),
    );
    mountRegions(renderer, store, sampleData());

    render();
    expect(context.fill).not.toHaveBeenCalled();

    store.set({ filters: { ...store.get().filters, showRegions: { 1: true, 2: false, 3: true } } });
    expect(renderer.scheduleRender).toHaveBeenCalled();
    render();
    expect(context.fill).toHaveBeenCalledTimes(1);

    context.fill.mockClear();
    store.set({ filters: { ...store.get().filters, regionMinNodes: 50 } }); // more than the department has
    render();
    expect(context.fill).not.toHaveBeenCalled();
  });

  it("deptAtViewport finds the department under a point only while regions are visible", () => {
    const ratio = { value: 1 };
    const { renderer } = fakeRenderer(ratio);
    const regions = mountRegions(renderer, new Store<AppState>(initialState()), sampleData());

    expect(regions.deptAtViewport({ x: 2, y: 2 })).toBe(0);
    expect(regions.deptAtViewport({ x: 202, y: 2 })).toBeNull(); // "no department" gets no region

    ratio.value = 0.1;
    expect(regions.deptAtViewport({ x: 2, y: 2 })).toBeNull();
  });

  it("when a node is selected, other regions fade", () => {
    const { renderer, context, render } = fakeRenderer({ value: 1 });
    const data = sampleData();
    data.authors.push(...cluster(100, 100, 16, 1).map((p, i) => authorNode(`B${i}`, p)));
    data.departments.push(department(1, "Лаборатория", "#00ff00"));
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: "A0" },
    });
    mountRegions(renderer, store, data);

    const alphas: Record<string, number> = {};
    context.fill.mockImplementation(() => {
      alphas[context.fillStyle] = context.globalAlpha;
    });
    render();

    expect(alphas["#00ff00"]).toBeLessThan(alphas["#ff0000"] ?? 0);
  });

  it("overlapping labels are not drawn: the label of the larger region stays", () => {
    const { renderer, context, render } = fakeRenderer({ value: 1 });
    const data = sampleData();
    // A larger second department right next to the first: their labels overlap on screen.
    data.authors.push(...cluster(10, 0, 30, 1).map((p, i) => authorNode(`B${i}`, p)));
    data.departments.push(department(1, "Лаборатория", "#00ff00"));
    mountRegions(renderer, new Store<AppState>(initialState()), data);

    render();

    const drawn = context.fillText.mock.calls.map(([text]) => text);
    expect(drawn).toEqual(["Лаборатория"]);
  });

  it("when a department is selected only its label is drawn", () => {
    const { renderer, context, render } = fakeRenderer({ value: 1 });
    const data = sampleData();
    data.authors.push(...cluster(500, 500, 16, 1).map((p, i) => authorNode(`B${i}`, p)));
    data.departments.push(department(1, "Лаборатория", "#00ff00"));
    const store = new Store<AppState>(initialState());
    mountRegions(renderer, store, data);

    render();
    expect(context.fillText).toHaveBeenCalledTimes(2); // far apart: both labels

    context.fillText.mockClear();
    store.set({ selection: { kind: "dept", id: 1 } });
    render();
    expect(context.fillText.mock.calls.map(([text]) => text)).toEqual(["Лаборатория"]);
  });

  it("hovering in region mode highlights the region and sets a hand cursor, closer than the threshold it does not react", () => {
    const ratio = { value: 1 };
    const { renderer, context, container, moveMouse } = fakeRenderer(ratio);
    mountRegions(renderer, new Store<AppState>(initialState()), sampleData());
    const alphas: number[] = [];
    context.fill.mockImplementation(() => alphas.push(context.globalAlpha));

    moveMouse(2, 2);
    expect(container.style.cursor).toBe("pointer");
    expect(alphas.at(-1)).toBe(REGION_CONFIG.hoverFillAlpha);

    moveMouse(150, 150); // empty spot
    expect(container.style.cursor).toBe("");
    expect(alphas.at(-1)).toBe(REGION_CONFIG.fillAlpha);

    ratio.value = 0.1;
    moveMouse(2, 2);
    expect(container.style.cursor).toBe("");
  });

  it("when a node or edge is selected region labels are not drawn even in region mode", () => {
    const { renderer, context, render } = fakeRenderer({ value: 1 });
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: "A0" },
    });
    mountRegions(renderer, store, sampleData());

    render();
    expect(context.fill).toHaveBeenCalled(); // regions themselves are drawn
    expect(context.fillText).not.toHaveBeenCalled();

    store.set({ selection: { kind: "edge", s: "A0", t: "A1", w: 1 } });
    context.fillText.mockClear();
    render();
    expect(context.fillText).not.toHaveBeenCalled();
  });

  it("the selected region keeps its fill and label when zooming in, the others stay outline-only", () => {
    const ratio = { value: 1 };
    const { renderer, context, render } = fakeRenderer(ratio);
    const data = sampleData();
    data.authors.push(...cluster(500, 500, 16, 1).map((p, i) => authorNode(`B${i}`, p)));
    data.departments.push(department(1, "Лаборатория", "#00ff00"));
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "dept", id: 1 } });
    mountRegions(renderer, store, data);
    const filled: string[] = [];
    context.fill.mockImplementation(() => filled.push(context.fillStyle));

    ratio.value = 0.1; // node mode
    render();

    expect(filled).toEqual(["#00ff00"]);
    expect(context.fillText.mock.calls.map(([text]) => text)).toEqual(["Лаборатория"]);
    expect(context.stroke).toHaveBeenCalledTimes(2); // both regions stroked
  });
});
