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

/** Плотное скопление `count` узлов департамента `dept` вокруг точки (cx, cy), по сетке с шагом `step`. */
function cluster(cx: number, cy: number, count: number, dept: number, step = 2): RegionPoint[] {
  const side = Math.ceil(Math.sqrt(count));
  return Array.from({ length: count }, (_, i) => ({
    x: cx + (i % side) * step,
    y: cy + Math.floor(i / side) * step,
    dept,
  }));
}

describe("buildRegions", () => {
  it("два далёких скопления разных департаментов дают по региону на каждый, и каждый накрывает свои узлы", () => {
    const a = cluster(0, 0, 16, 1);
    const b = cluster(200, 0, 16, 2);

    const regions = buildRegions([...a, ...b], 10);

    expect(regions.map((r) => r.dept)).toEqual([1, 2]);
    for (const p of a) expect(regionDeptAt(regions, p)).toBe(1);
    for (const p of b) expect(regionDeptAt(regions, p)).toBe(2);
    expect(regionDeptAt(regions, { x: 100, y: 0 })).toBeNull(); // пустота между скоплениями не закрашена
  });

  it("соседние департаменты не перекрываются: точка не лежит сразу в двух регионах", () => {
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

  it("остров меньше minNodes отбрасывается, а крупный остров того же департамента остаётся", () => {
    const big = cluster(0, 0, 20, 1);
    const small = cluster(300, 0, 4, 1);

    const regions = buildRegions([...big, ...small], 10);

    expect(regions).toHaveLength(1);
    expect(regionDeptAt(regions, big[0] ?? { x: 0, y: 0 })).toBe(1);
    expect(regionDeptAt(regions, small[0] ?? { x: 0, y: 0 })).toBeNull();
  });

  it("несколько крупных островов одного департамента рисуются все, а не только самый большой", () => {
    const regions = buildRegions([...cluster(0, 0, 20, 1), ...cluster(300, 0, 12, 1)], 10);

    expect(regions).toHaveLength(1);
    expect(regionDeptAt(regions, { x: 2, y: 2 })).toBe(1);
    expect(regionDeptAt(regions, { x: 302, y: 2 })).toBe(1);
    expect(regions[0]?.rings.length).toBeGreaterThanOrEqual(2);
  });

  it("точка названия — центр узлов самого крупного острова, а не всех узлов департамента", () => {
    const big = cluster(0, 0, 20, 1); // 4 ряда по 5 узлов: x 0..8, y 0..6 -> центр (4, 3)
    const regions = buildRegions([...big, ...cluster(300, 0, 12, 1)], 10);

    expect(regions[0]?.label.x).toBeCloseTo(4);
    expect(regions[0]?.label.y).toBeCloseTo(3);
  });

  it("департамент, у которого узлов меньше minNodes вообще, в результат не попадает; пустой вход — пустой результат", () => {
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

/** Департамент 0 — скопление у (0, 0); «Без департамента» (9) — скопление у (200, 0). */
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
    selection: null,
    filters: {
      minCoauth: 1,
      minSharedAuthors: 1,
      yearMax: 2026,
      showNoDeptAuthors: true,
      showNoDeptPubs: true,
      edgeZoomThreshold: 0.25,
      showRegions: { 1: true, 2: false, 3: true },
      regionZoomThreshold: 0.25,
      regionMinNodes: 10,
      ...overrides,
    },
  };
}

/**
 * Фейковый Sigma-рендерер: canvas с подменённым 2d-контекстом (jsdom его не
 * рисует), координаты раскладки = координаты экрана, afterRender вызывается тестом.
 */
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
  // Оба canvas (заливка и названия) отдают один и тот же фейковый контекст.
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

  it("переносит по словам, не превышая ширину строки", () => {
    expect(wrapLabel("Институт прикладных компьютерных наук", measure, 200, 3)).toEqual([
      "Институт прикладных",
      "компьютерных наук",
    ]);
  });

  it("строк не больше maxLines, последняя обрезается многоточием и тоже влезает в ширину", () => {
    const lines = wrapLabel("один два три четыре пять шесть семь", measure, 80, 2);
    expect(lines).toHaveLength(2);
    expect(lines[1]?.endsWith("…")).toBe(true);
    expect(measure(lines[1] ?? "")).toBeLessThanOrEqual(80);
  });

  it("слово длиннее ширины остаётся целым", () => {
    expect(wrapLabel("Сверхдлинноеслово", measure, 50, 3)).toEqual(["Сверхдлинноеслово"]);
  });
});

describe("mountRegions", () => {
  it("кладёт свой canvas под рёбра и рисует регионы, пока камера дальше порога", () => {
    const ratio = { value: 1 };
    const { renderer, context, render } = fakeRenderer(ratio);
    mountRegions(renderer, new Store<AppState>(initialState()), sampleData());

    expect(renderer.createCanvas).toHaveBeenCalledWith("regions", { beforeLayer: "edges" });
    expect(renderer.createCanvas).toHaveBeenCalledWith("region-labels", { afterLayer: "labels" });
    render();
    // Название — в центре острова (скопление 4×4 с шагом 2 у (0, 0) -> центр (3, 3)), на текущем языке.
    expect(context.fillText).toHaveBeenCalledWith("Кафедра", 3, 3);
    expect(context.fill).toHaveBeenCalledWith("evenodd");
    expect(context.fillStyle).toBe("#ff0000"); // только реальный департамент, не «Без департамента»
    expect(context.fill).toHaveBeenCalledTimes(1);

    context.fill.mockClear();
    context.fillText.mockClear();
    context.stroke.mockClear();
    ratio.value = 0.2; // ближе порога 0.25 — остаётся только обводка, без заливки и названий
    render();
    expect(context.clearRect).toHaveBeenCalled();
    expect(context.stroke).toHaveBeenCalled();
    expect(context.fill).not.toHaveBeenCalled();
    expect(context.fillText).not.toHaveBeenCalled();
  });

  it("не рисует на вкладке, где регионы выключены, и пересчитывается при смене фильтров", () => {
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
    store.set({ filters: { ...store.get().filters, regionMinNodes: 50 } }); // больше, чем узлов у департамента
    render();
    expect(context.fill).not.toHaveBeenCalled();
  });

  it("deptAtViewport находит департамент под точкой, только пока регионы видны", () => {
    const ratio = { value: 1 };
    const { renderer } = fakeRenderer(ratio);
    const regions = mountRegions(renderer, new Store<AppState>(initialState()), sampleData());

    expect(regions.deptAtViewport({ x: 2, y: 2 })).toBe(0);
    expect(regions.deptAtViewport({ x: 202, y: 2 })).toBeNull(); // «Без департамента» региона не получает

    ratio.value = 0.1;
    expect(regions.deptAtViewport({ x: 2, y: 2 })).toBeNull();
  });

  it("при выборе узла чужие регионы притухают", () => {
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

  it("перекрывающиеся названия не рисуются: остаётся название более крупного региона", () => {
    const { renderer, context, render } = fakeRenderer({ value: 1 });
    const data = sampleData();
    // Второй департамент вплотную к первому и крупнее — названия в экранных координатах накладываются.
    data.authors.push(...cluster(10, 0, 30, 1).map((p, i) => authorNode(`B${i}`, p)));
    data.departments.push(department(1, "Лаборатория", "#00ff00"));
    mountRegions(renderer, new Store<AppState>(initialState()), data);

    render();

    const drawn = context.fillText.mock.calls.map(([text]) => text);
    expect(drawn).toEqual(["Лаборатория"]);
  });

  it("при выбранном департаменте рисуется только его название", () => {
    const { renderer, context, render } = fakeRenderer({ value: 1 });
    const data = sampleData();
    data.authors.push(...cluster(500, 500, 16, 1).map((p, i) => authorNode(`B${i}`, p)));
    data.departments.push(department(1, "Лаборатория", "#00ff00"));
    const store = new Store<AppState>(initialState());
    mountRegions(renderer, store, data);

    render();
    expect(context.fillText).toHaveBeenCalledTimes(2); // далеко друг от друга — оба названия

    context.fillText.mockClear();
    store.set({ selection: { kind: "dept", id: 1 } });
    render();
    expect(context.fillText.mock.calls.map(([text]) => text)).toEqual(["Лаборатория"]);
  });

  it("наведение в режиме регионов подсвечивает регион и ставит курсор-руку, ближе порога — не реагирует", () => {
    const ratio = { value: 1 };
    const { renderer, context, container, moveMouse } = fakeRenderer(ratio);
    mountRegions(renderer, new Store<AppState>(initialState()), sampleData());
    const alphas: number[] = [];
    context.fill.mockImplementation(() => alphas.push(context.globalAlpha));

    moveMouse(2, 2);
    expect(container.style.cursor).toBe("pointer");
    expect(alphas.at(-1)).toBe(REGION_CONFIG.hoverFillAlpha);

    moveMouse(150, 150); // пусто
    expect(container.style.cursor).toBe("");
    expect(alphas.at(-1)).toBe(REGION_CONFIG.fillAlpha);

    ratio.value = 0.1;
    moveMouse(2, 2);
    expect(container.style.cursor).toBe("");
  });

  it("при выбранном узле или ребре названия регионов не рисуются даже в режиме регионов", () => {
    const { renderer, context, render } = fakeRenderer({ value: 1 });
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: "A0" },
    });
    mountRegions(renderer, store, sampleData());

    render();
    expect(context.fill).toHaveBeenCalled(); // сами регионы видны
    expect(context.fillText).not.toHaveBeenCalled();

    store.set({ selection: { kind: "edge", s: "A0", t: "A1", w: 1 } });
    context.fillText.mockClear();
    render();
    expect(context.fillText).not.toHaveBeenCalled();
  });

  it("выбранный регион держит заливку и название при приближении, остальные остаются только обводкой", () => {
    const ratio = { value: 1 };
    const { renderer, context, render } = fakeRenderer(ratio);
    const data = sampleData();
    data.authors.push(...cluster(500, 500, 16, 1).map((p, i) => authorNode(`B${i}`, p)));
    data.departments.push(department(1, "Лаборатория", "#00ff00"));
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "dept", id: 1 } });
    mountRegions(renderer, store, data);
    const filled: string[] = [];
    context.fill.mockImplementation(() => filled.push(context.fillStyle));

    ratio.value = 0.1; // режим узлов
    render();

    expect(filled).toEqual(["#00ff00"]);
    expect(context.fillText.mock.calls.map(([text]) => text)).toEqual(["Лаборатория"]);
    expect(context.stroke).toHaveBeenCalledTimes(2); // обводка у обоих регионов
  });
});
