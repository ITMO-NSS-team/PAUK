import Graph from "graphology";
import type Sigma from "sigma";
import { describe, expect, it, vi } from "vitest";
import type { GraphData, PubDetail } from "../src/contracts/graph";
import { MAP_CONFIG, NO_DEPT_COLOR } from "../src/core/config";
import { darkTheme } from "../src/core/themes/dark";
import { indexDetailsByKey } from "../src/core/data";
import { Store, type AppState } from "../src/core/state";
import { loadSampleGraphData, loadSamplePubDetails } from "./fixtures";
import {
  deptNodeKey,
  isolatedAuthors,
  mountReactiveGraph,
  mountZoomDebug,
  parseDeptNodeKey,
  populateGraph,
} from "../src/map/build";

// Thresholds that filter nothing; edgeZoomThreshold 10 is above any test camera ratio.
const NO_FILTER = {
  minCoauth: 1,
  minSharedAuthors: 1,
  yearMax: 2026,
  showNoDeptAuthors: true,
  showNoDeptPubs: true,
  showExternalAuthors: false,
  showIsolatedAuthors: true,
  edgeZoomThreshold: 10,
  showRegions: { 1: false, 2: false, 3: false },
  regionZoomThreshold: 0.25,
  regionMinNodes: 10,
};
// Empty map: nodeLabel() falls back to the pub key.
const NO_PUB_DETAILS = new Map<string, PubDetail>();

const NODE_BASE = {
  x: 0,
  y: 0,
  size: MAP_CONFIG.node.radius,
  color: "#fff",
  label: "L",
  hidden: false,
};
const EDGE_BASE = {
  size: MAP_CONFIG.edge.width,
  color: darkTheme.map.edge,
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
    theme: "dark",
    selection: null,
    filters: NO_FILTER,
    ...overrides,
  };
}

/** Without department anchor nodes. */
function realNodeKeys(graph: Graph): string[] {
  return graph.nodes().filter((key) => parseDeptNodeKey(key) === null);
}

/** Without department edges. */
function realEdgeKeys(graph: Graph): string[] {
  return graph
    .edges()
    .filter((edgeKey) => parseDeptNodeKey(graph.extremities(edgeKey)[0]) === null);
}

/** One real department and one "no department"; separate from the shared fixture, whose counts other tests rely on. */
function dataWithNoDept(): GraphData {
  return {
    departments: [
      {
        id: 0,
        name: "ИТМО",
        name_en: "ITMO",
        color: "#c27070",
        n: 1,
        n_authors: 1,
        n_pubs: 1,
        n_repos: 0,
      },
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
      {
        key: "A1",
        kind: "author",
        dept: 0,
        label: "Иванов",
        label_en: "Ivanov",
        pubs_count: 1,
        rank: 1,
        gx: 100,
        gy: 100,
      },
      {
        key: "A2",
        kind: "author",
        dept: 1,
        label: "Петров",
        label_en: "Petrov",
        pubs_count: 1,
        rank: 1,
        gx: 200,
        gy: 200,
      },
    ],
    coauth_edges: [],
    repos: [],
    repo_edges: [],
    repo_author_edges: [],
    repo_pub_edges: [],
    pubs: [
      {
        key: "P1",
        kind: "pub",
        dept: 0,
        depts: [0],
        year: 2024,
        n_authors: 1,
        rank: 1,
        gx: 100,
        gy: 100,
      },
      {
        key: "P2",
        kind: "pub",
        dept: 1,
        depts: [1],
        year: 2024,
        n_authors: 1,
        rank: 1,
        gx: 200,
        gy: 200,
      },
    ],
    pub_edges: [],
    all_edges: [],
  };
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Reducer = (...args: any[]) => unknown;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type EventHandler = (...args: any[]) => void;

/** Offset proving flyToSelection reads getNodeDisplayData, not graph attributes. */
const FRAMED_COORD_OFFSET = 1000;

/** Keeps a real graph and stores settings and event callbacks so tests can call them; real Sigma needs WebGL. */
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
    // Differs from the raw attributes, like Sigma's normalized display data.
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
      if (!reducer) throw new Error(`reducer "${key}" is not registered yet`);
      return reducer;
    },
    fire: (event, payload) => handlers.get(event)?.(payload),
    fireCameraUpdated: (ratio) => {
      cameraRatio = ratio;
      cameraHandlers.get("updated")?.({ ratio, x: 0, y: 0, angle: 0 });
    },
  };
}

describe("populateGraph on fixture data", () => {
  it("returns only the tab's nodes, not all entities at once", async () => {
    const data = await loadSampleGraphData();

    // Three separate graphs: kinds never mix in one tab.
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

  it("colours nodes by their department colour", async () => {
    const data = await loadSampleGraphData();
    const graph = new Graph();
    populateGraph(graph, data, "ru", 1, NO_FILTER, NO_PUB_DETAILS);
    const deptColor = new Map(data.departments.map((d) => [d.id, d.color]));

    for (const key of realNodeKeys(graph)) {
      const author = data.authors.find((a) => a.key === key);
      expect(graph.getNodeAttribute(key, "color")).toBe(deptColor.get(author?.dept ?? -1));
    }
  });

  it("substitutes the real publication title from pubDetails instead of the key (accounting for map truncation, see labelMaxLength)", async () => {
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

  it("truncates a long label to MAP_CONFIG.node.labelMaxLength with an ellipsis", async () => {
    const data = await loadSampleGraphData();
    const longTitle = "А".repeat(MAP_CONFIG.node.labelMaxLength + 10);
    const pub = data.pubs[0];
    if (!pub) throw new Error("the fixture must contain at least one publication");
    const pubDetails = new Map([[pub.key, { key: pub.key, label: longTitle } as PubDetail]]);

    const graph = new Graph();
    populateGraph(graph, data, "ru", 3, NO_FILTER, pubDetails);

    const label = graph.getNodeAttribute(pub.key, "label") as string;
    expect(label.length).toBe(MAP_CONFIG.node.labelMaxLength);
    expect(label.endsWith("…")).toBe(true);
  });

  it("returns only the edges of its own tab", async () => {
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

  it("filters.minCoauth hides weak coauthorship links on tab 1", async () => {
    const data = await loadSampleGraphData();
    const strong = data.coauth_edges.filter((e) => e.w >= 2).length;

    const graph = new Graph();
    populateGraph(graph, data, "ru", 1, { ...NO_FILTER, minCoauth: 2 }, NO_PUB_DETAILS);
    expect(realEdgeKeys(graph)).toHaveLength(strong);
    expect(strong).toBeLessThan(data.coauth_edges.length); // the fixture has a spread of weights
  });

  it("filters.minSharedAuthors hides weak publication links on tab 3", async () => {
    const data = await loadSampleGraphData();
    const strong = data.pub_edges.filter((e) => e.w >= 2).length;

    const graph = new Graph();
    populateGraph(graph, data, "ru", 3, { ...NO_FILTER, minSharedAuthors: 2 }, NO_PUB_DETAILS);
    expect(realEdgeKeys(graph)).toHaveLength(strong);
  });

  it("filters.yearMax hides publications later than the given year, but not publications with no known year", async () => {
    const data = await loadSampleGraphData();
    const filters = { ...NO_FILTER, yearMax: 2022 };
    const expectedPubs = data.pubs.filter((p) => p.year === null || p.year <= 2022);

    const graph = new Graph();
    populateGraph(graph, data, "ru", 3, filters, NO_PUB_DETAILS);
    expect(realNodeKeys(graph).sort()).toEqual(expectedPubs.map((p) => p.key).sort());
    expect(expectedPubs.some((p) => p.year === null)).toBe(true); // the fixture has a pub without a year

    // Edges to a pub hidden by the year filter disappear too.
    for (const edgeKey of realEdgeKeys(graph)) {
      const [s, t] = graph.extremities(edgeKey);
      expect(realNodeKeys(graph)).toContain(s);
      expect(realNodeKeys(graph)).toContain(t);
    }
  });

  it("filters.showNoDeptAuthors=false hides authors of the synthetic \"Без департамента\" department", () => {
    const data = dataWithNoDept();

    const shown = new Graph();
    populateGraph(shown, data, "ru", 1, NO_FILTER, NO_PUB_DETAILS);
    const hidden = new Graph();
    populateGraph(
      hidden,
      data,
      "ru",
      1,
      { ...NO_FILTER, showNoDeptAuthors: false },
      NO_PUB_DETAILS,
    );

    expect(realNodeKeys(shown)).toEqual(["A1", "A2"]);
    expect(realNodeKeys(hidden)).toEqual(["A1"]); // A2 is in "no department"
  });

  it("external authors (is_itmo: false) are hidden while filters.showExternalAuthors is off", () => {
    const data = dataWithNoDept();
    data.authors = data.authors.map((a) => (a.key === "A1" ? { ...a, is_itmo: false } : a));

    const hidden = new Graph();
    populateGraph(hidden, data, "ru", 1, NO_FILTER, NO_PUB_DETAILS);
    const shown = new Graph();
    populateGraph(
      shown,
      data,
      "ru",
      1,
      { ...NO_FILTER, showExternalAuthors: true },
      NO_PUB_DETAILS,
    );

    expect(realNodeKeys(hidden)).toEqual(["A2"]);
    expect(realNodeKeys(shown)).toEqual(["A1", "A2"]);
  });

  it("isolatedAuthors: one publication, no coauthors and no repositories; an external coauthor counts as a link, only while externals are shown", () => {
    const data = {
      authors: [
        { key: "A1", pubs_count: 1 }, // alone on P1, only external E1 nearby
        { key: "A2", pubs_count: 1 }, // with A3 on P2
        { key: "A3", pubs_count: 1 },
        { key: "A4", pubs_count: 1 }, // alone on P3, but has a repo
        { key: "A5", pubs_count: 2 }, // alone, but two pubs
        { key: "E1", pubs_count: 1, is_itmo: false },
      ],
      all_edges: [
        { s: "A1", t: "P1" },
        { s: "E1", t: "P1" },
        { s: "A2", t: "P2" },
        { s: "A3", t: "P2" },
        { s: "A4", t: "P3" },
        { s: "A5", t: "P4" },
        { s: "A5", t: "P5" },
      ],
      repo_author_edges: [{ s: "R1", t: "A4", role: "owner" }],
    } as unknown as GraphData;

    // External hidden: A1's only co-author is external E1, so A1 is isolated; E1 is not on the map.
    expect([...isolatedAuthors(data, false)]).toEqual(["A1"]);
    // External shown: A1 and E1 are co-authors.
    expect([...isolatedAuthors(data, true)]).toEqual([]);
  });

  it("authors without links are hidden from the map while filters.showIsolatedAuthors is off", () => {
    const data = dataWithNoDept();
    const isolated = [...isolatedAuthors(data, NO_FILTER.showExternalAuthors)];
    if (isolated.length === 0) throw new Error("the fixture must contain an author without links");

    const hidden = new Graph();
    populateGraph(
      hidden,
      data,
      "ru",
      1,
      { ...NO_FILTER, showIsolatedAuthors: false },
      NO_PUB_DETAILS,
    );
    const shown = new Graph();
    populateGraph(shown, data, "ru", 1, NO_FILTER, NO_PUB_DETAILS);

    for (const key of isolated) {
      expect(realNodeKeys(hidden)).not.toContain(key);
      expect(realNodeKeys(shown)).toContain(key);
    }
  });

  it("an author whose coauthors are all external appears together with the externals, even when \"без связей\" authors are hidden", () => {
    const base = dataWithNoDept();
    const [author] = base.authors;
    if (!author) throw new Error("the fixture must contain an author");
    const external = { ...author, key: "E1", is_itmo: false };
    const data: GraphData = {
      ...base,
      authors: [{ ...author, pubs_count: 1 }, external],
      all_edges: [
        { s: author.key, t: "P_SOLO" },
        { s: "E1", t: "P_SOLO" },
      ],
      repo_author_edges: [],
    };
    const hideIsolated = { ...NO_FILTER, showIsolatedAuthors: false };

    const externalsOff = new Graph();
    populateGraph(externalsOff, data, "ru", 1, hideIsolated, NO_PUB_DETAILS);
    const externalsOn = new Graph();
    populateGraph(
      externalsOn,
      data,
      "ru",
      1,
      { ...hideIsolated, showExternalAuthors: true },
      NO_PUB_DETAILS,
    );

    expect(realNodeKeys(externalsOff)).toEqual([]);
    expect(realNodeKeys(externalsOn)).toEqual([author.key, "E1"]);
  });

  it("an author without an is_itmo field (graph-data.json from before external authors) counts as ITMO and is always visible", () => {
    const graph = new Graph();
    populateGraph(graph, dataWithNoDept(), "ru", 1, NO_FILTER, NO_PUB_DETAILS);
    expect(realNodeKeys(graph)).toEqual(["A1", "A2"]);
  });

  it("filters.showNoDeptPubs=false hides publications of the synthetic \"Без департамента\" department", () => {
    const data = dataWithNoDept();

    const shown = new Graph();
    populateGraph(shown, data, "ru", 3, NO_FILTER, NO_PUB_DETAILS);
    const hidden = new Graph();
    populateGraph(hidden, data, "ru", 3, { ...NO_FILTER, showNoDeptPubs: false }, NO_PUB_DETAILS);

    expect(realNodeKeys(shown)).toEqual(["P1", "P2"]);
    expect(realNodeKeys(hidden)).toEqual(["P1"]); // P2 is in "no department"
  });
});

describe("deptNodeKey / parseDeptNodeKey", () => {
  it("parsing returns the same number that was encoded", () => {
    for (const id of [0, 1, 42]) {
      expect(parseDeptNodeKey(deptNodeKey(id))).toBe(id);
    }
  });

  it("a regular node key is not a department", () => {
    expect(parseDeptNodeKey("A5133538481")).toBeNull();
    expect(parseDeptNodeKey("example-org/graph-toolkit")).toBeNull();
  });
});

describe("populateGraph department label anchors (not a blob in place of real nodes)", () => {
  it("adds one anchor per department that has at least one node in this tab", async () => {
    const data = await loadSampleGraphData();
    const graph = new Graph();
    populateGraph(graph, data, "ru", 1, NO_FILTER, NO_PUB_DETAILS);

    const deptsWithAuthors = new Set(data.authors.map((a) => a.dept));
    for (const deptId of deptsWithAuthors) {
      expect(graph.hasNode(deptNodeKey(deptId))).toBe(true);
    }
  });

  it("an anchor is invisible (size: 0) and does not replace real nodes with a blob", async () => {
    const data = await loadSampleGraphData();
    const graph = new Graph();
    populateGraph(graph, data, "ru", 1, NO_FILTER, NO_PUB_DETAILS);

    const [deptId] = [...new Set(data.authors.map((a) => a.dept))];
    if (deptId === undefined) throw new Error("the fixture must contain at least one author");
    expect(graph.getNodeAttribute(deptNodeKey(deptId), "size")).toBe(0);
  });

  it("the anchor position is the centroid of the department's nodes in this tab", async () => {
    const data = await loadSampleGraphData();
    const graph = new Graph();
    populateGraph(graph, data, "ru", 1, NO_FILTER, NO_PUB_DETAILS);

    const [deptId] = [...new Set(data.authors.map((a) => a.dept))];
    if (deptId === undefined) throw new Error("the fixture must contain at least one author");
    const deptAuthors = data.authors.filter((a) => a.dept === deptId);

    expect(graph.getNodeAttribute(deptNodeKey(deptId), "x")).toBeCloseTo(
      deptAuthors.reduce((sum, a) => sum + a.gx, 0) / deptAuthors.length,
    );
    expect(graph.getNodeAttribute(deptNodeKey(deptId), "y")).toBeCloseTo(
      deptAuthors.reduce((sum, a) => sum + a.gy, 0) / deptAuthors.length,
    );
  });

  it("edges between departments come from data.dept_edges instead of being recomputed", async () => {
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

describe("applyGraphStyling (via mountReactiveGraph) department anchors", () => {
  it("a department anchor is hidden, has no label and is not highlighted, at any zoom and when the department itself is selected", async () => {
    const data = await loadSampleGraphData();
    const graph = new Graph();
    graph.addNode("A1", { x: 0, y: 0, dept: 0 });
    graph.addNode(deptNodeKey(0), { x: 0, y: 0, size: 0 });
    const store = new Store<AppState>(initialState({ selection: { kind: "dept", id: 0 } }));
    const { renderer, getReducer, fireCameraUpdated } = fakeRenderer(graph);

    mountReactiveGraph(renderer, store, data, NO_PUB_DETAILS);
    const nodeReducer = getReducer("nodeReducer");

    for (const ratio of [0.1, 1, 2]) {
      fireCameraUpdated(ratio);
      expect(nodeReducer(deptNodeKey(0), { ...NODE_BASE, size: 0 })).toMatchObject({
        hidden: true,
        label: "",
        forceLabel: false,
        highlighted: false,
        size: 0,
      });
    }
  });
});

describe("applyGraphStyling (via mountReactiveGraph) grant selection", () => {
  it("grant papers stay bright, the rest are dimmed, edges are hidden", async () => {
    const data = await loadSampleGraphData();
    const [detail] = await loadSamplePubDetails();
    if (!detail) throw new Error("the fixture has no publication details");
    const pubDetails = indexDetailsByKey([
      { ...detail, key: "P1", funding: [{ funder: "RSF", grant_id: "18-19-00627", grant_key: "18-19-00627" }] },
      { ...detail, key: "P2", funding: [] },
    ]);
    const graph = new Graph();
    graph.addNode("P1", { x: 0, y: 0 });
    graph.addNode("P2", { x: 1, y: 1 });
    graph.addEdge("P1", "P2");
    const store = new Store<AppState>(initialState({ selection: { kind: "grant", key: "18-19-00627" } }));
    const { renderer, getReducer } = fakeRenderer(graph);

    mountReactiveGraph(renderer, store, data, pubDetails);

    // A pub of the grant is bright and labelled at any zoom.
    expect(getReducer("nodeReducer")("P1", NODE_BASE)).toMatchObject({
      color: NODE_BASE.color,
      forceLabel: true,
    });
    expect(getReducer("nodeReducer")("P2", NODE_BASE)).toMatchObject({ color: darkTheme.map.dimNode });
    expect(getReducer("edgeReducer")(graph.edges()[0], EDGE_BASE)).toMatchObject({ hidden: true });
  });
});

describe("applyGraphStyling (via mountReactiveGraph) selection/hover and neighbour dimming", () => {
  it("nodeReducer highlights the selected node, keeps neighbours at normal size with a forced label, dims the rest", async () => {
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

    expect(nodeReducer("A1", NODE_BASE)).toMatchObject({
      highlighted: true,
      size: MAP_CONFIG.node.radiusSelected,
    });
    // Neighbour: same color and size, but a forced label.
    expect(nodeReducer("A2", NODE_BASE)).toMatchObject({
      color: NODE_BASE.color,
      label: NODE_BASE.label,
      size: NODE_BASE.size,
      forceLabel: true,
    });
    expect(nodeReducer("A3", NODE_BASE)).toMatchObject({
      color: darkTheme.map.dimNode,
      label: "",
    }); // not a neighbour: dimmed
  });

  it("selection on a key that is not in the CURRENT graph (e.g. after a tab change) does not crash the reducer", async () => {
    // Regression: areNeighbors() threw on a focus missing from the graph (e.g.
    // a selection surviving a tab switch). The reducer must be safe on its own.
    const data = await loadSampleGraphData();
    const graph = new Graph();
    graph.addNode("A2", { x: 1, y: 1 });
    const store = new Store<AppState>(initialState({ selection: { kind: "node", key: "A1" } })); // "A1" is not in the graph
    const { renderer, getReducer } = fakeRenderer(graph);

    mountReactiveGraph(renderer, store, data, NO_PUB_DETAILS);

    expect(() => getReducer("nodeReducer")("A2", NODE_BASE)).not.toThrow();
  });

  it("with no selection and no hover nothing is dimmed", async () => {
    const data = await loadSampleGraphData();
    const graph = new Graph();
    graph.addNode("A1", { x: 0, y: 0 });
    graph.addNode("A2", { x: 1, y: 1 });
    const store = new Store<AppState>(initialState());
    const { renderer, getReducer } = fakeRenderer(graph);

    mountReactiveGraph(renderer, store, data, NO_PUB_DETAILS);
    expect(getReducer("nodeReducer")("A2", NODE_BASE)).toEqual(NODE_BASE);
  });

  it("mouse hover (enterNode) sets focus only when nothing is selected by click; leaveNode clears it", async () => {
    const data = await loadSampleGraphData();
    const graph = new Graph();
    graph.addNode("A1", { x: 0, y: 0 });
    graph.addNode("A2", { x: 1, y: 1 });
    graph.addNode("A3", { x: 2, y: 2 });
    graph.addEdge("A2", "A3");
    const store = new Store<AppState>(initialState()); // nothing selected
    const { renderer, getReducer, fire } = fakeRenderer(graph);

    mountReactiveGraph(renderer, store, data, NO_PUB_DETAILS);
    const nodeReducer = getReducer("nodeReducer");

    fire("enterNode", { node: "A2" });
    // Hover neighbour: not dimmed, not enlarged, no forced label.
    const a3 = nodeReducer("A3", NODE_BASE);
    expect(a3).toMatchObject({ color: NODE_BASE.color, size: NODE_BASE.size });
    expect(a3).not.toHaveProperty("forceLabel", true);
    expect(nodeReducer("A1", NODE_BASE)).toMatchObject({
      color: darkTheme.map.dimNode,
      label: "",
    }); // not a neighbour: dimmed

    fire("leaveNode", {});
    expect(nodeReducer("A1", NODE_BASE)).toEqual(NODE_BASE); // hover gone: no focus
  });

  it("the selection stays the focus when hovering ANOTHER node, AND hover highlights ITS neighbours at the same time, both focus sources work independently", async () => {
    const data = await loadSampleGraphData();
    const graph = new Graph();
    graph.addNode("A1", { x: 0, y: 0 });
    graph.addNode("A2", { x: 1, y: 1 }); // neighbour of A1 (selected)
    graph.addNode("A3", { x: 2, y: 2 }); // neighbour of A4 (hovered), not of A1
    graph.addNode("A4", { x: 3, y: 3 });
    graph.addNode("A5", { x: 4, y: 4 }); // neither: stays dimmed
    graph.addEdge("A1", "A2");
    graph.addEdge("A3", "A4");
    const store = new Store<AppState>(initialState({ selection: { kind: "node", key: "A1" } }));
    const { renderer, getReducer, fire } = fakeRenderer(graph);

    mountReactiveGraph(renderer, store, data, NO_PUB_DETAILS);
    const nodeReducer = getReducer("nodeReducer");
    const edgeReducer = getReducer("edgeReducer");
    const [edgeA1A2] = graph.edges("A1", "A2");
    const [edgeA3A4] = graph.edges("A3", "A4");
    if (!edgeA1A2 || !edgeA3A4) throw new Error("the graph must contain both edges");

    // Hover A4 while A1 is selected: both focus sources stay active.
    fire("enterNode", { node: "A4" });

    expect(nodeReducer("A1", NODE_BASE)).toMatchObject({
      highlighted: true,
      size: MAP_CONFIG.node.radiusSelected,
    });
    expect(nodeReducer("A2", NODE_BASE)).toMatchObject({
      color: NODE_BASE.color,
      size: NODE_BASE.size, // neighbours never grow
      forceLabel: true, // selection neighbour: forced label
    });
    expect(edgeReducer(edgeA1A2, EDGE_BASE)).toMatchObject({ color: EDGE_BASE.color }); // selection edge still visible
    const a3 = nodeReducer("A3", NODE_BASE); // hover neighbour (A4) is lit too
    expect(a3).toMatchObject({ color: NODE_BASE.color, size: NODE_BASE.size });
    expect(a3).not.toHaveProperty("forceLabel", true); // no forced label for a hover neighbour
    expect(edgeReducer(edgeA3A4, EDGE_BASE)).toMatchObject({ color: EDGE_BASE.color }); // hover edge visible too
    expect(nodeReducer("A5", NODE_BASE)).toMatchObject({
      color: darkTheme.map.dimNode,
      label: "",
    }); // neither: dimmed
  });

  it("selecting a department keeps its nodes bright, dims the rest, shows no edges, a region has no node of its own", async () => {
    const data = await loadSampleGraphData();
    const graph = new Graph();
    graph.addNode("A1", { x: 0, y: 0, dept: 0 });
    graph.addNode("A2", { x: 1, y: 1, dept: 0 });
    graph.addNode("A3", { x: 2, y: 2, dept: 1 });
    graph.addNode("A4", { x: 3, y: 3, dept: 1 });
    graph.addNode(deptNodeKey(0), { x: 0, y: 0, size: 0 });
    graph.addEdge("A1", "A2");
    graph.addEdge("A2", "A3");
    graph.addEdge("A3", "A4");
    const store = new Store<AppState>(initialState({ selection: { kind: "dept", id: 0 } }));
    const { renderer, getReducer } = fakeRenderer(graph);

    mountReactiveGraph(renderer, store, data, NO_PUB_DETAILS);
    const nodeReducer = getReducer("nodeReducer");
    const edgeReducer = getReducer("edgeReducer");

    expect(nodeReducer("A1", NODE_BASE)).toMatchObject({
      color: NODE_BASE.color,
      label: NODE_BASE.label,
    });
    expect(nodeReducer("A1", NODE_BASE)).not.toMatchObject({ highlighted: true });
    expect(nodeReducer("A3", NODE_BASE)).toMatchObject({
      color: darkTheme.map.dimNode,
      label: "",
    });

    const edgeKey = (s: string, t: string) => graph.edge(s, t) ?? "";
    expect(edgeReducer(edgeKey("A1", "A2"), EDGE_BASE)).toMatchObject({ hidden: true }); // both ends in the department
    expect(edgeReducer(edgeKey("A2", "A3"), EDGE_BASE)).toMatchObject({ hidden: true });
    expect(edgeReducer(edgeKey("A3", "A4"), EDGE_BASE)).toMatchObject({ hidden: true }); // both ends outside
  });

  it("selecting an EDGE highlights both its endpoints with labels, like neighbours of a node selection, but does not enlarge them or mark them highlighted", async () => {
    const data = await loadSampleGraphData();
    const graph = new Graph();
    graph.addNode("A1", { x: 0, y: 0 });
    graph.addNode("A2", { x: 1, y: 1 });
    graph.addNode("A3", { x: 2, y: 2 }); // not on the selected edge: stays dimmed
    graph.addEdge("A1", "A2", { weight: 2 });
    const store = new Store<AppState>(
      initialState({ selection: { kind: "edge", s: "A1", t: "A2", w: 2 } }),
    );
    const { renderer, getReducer } = fakeRenderer(graph);

    mountReactiveGraph(renderer, store, data, NO_PUB_DETAILS);
    const nodeReducer = getReducer("nodeReducer");

    for (const key of ["A1", "A2"]) {
      const res = nodeReducer(key, NODE_BASE);
      expect(res).toMatchObject({ color: NODE_BASE.color, size: NODE_BASE.size, forceLabel: true });
      expect(res).not.toHaveProperty("highlighted", true); // edge endpoints, not the selected node
    }
    expect(nodeReducer("A3", NODE_BASE)).toMatchObject({
      color: darkTheme.map.dimNode,
      label: "",
    });
  });

  it("selecting an EDGE keeps OTHER edges of its endpoints visible (and does not dim the nodes at the other end of those edges), not only the selected segment", async () => {
    const data = await loadSampleGraphData();
    const graph = new Graph();
    graph.addNode("A1", { x: 0, y: 0 });
    graph.addNode("A2", { x: 1, y: 1 });
    graph.addNode("A5", { x: 4, y: 4 }); // neighbour of A1 via another edge
    graph.addNode("A6", { x: 5, y: 5 });
    graph.addNode("A7", { x: 6, y: 6 });
    graph.addEdge("A1", "A2", { weight: 2 }); // selected edge
    graph.addEdge("A1", "A5", { weight: 1 }); // another edge of the same endpoint: stays visible
    graph.addEdge("A6", "A7", { weight: 1 }); // unrelated to A1 and A2: hidden
    const store = new Store<AppState>(
      initialState({ selection: { kind: "edge", s: "A1", t: "A2", w: 2 } }),
    );
    const { renderer, getReducer } = fakeRenderer(graph);

    mountReactiveGraph(renderer, store, data, NO_PUB_DETAILS);
    const nodeReducer = getReducer("nodeReducer");
    const edgeReducer = getReducer("edgeReducer");
    const [edgeA1A5] = graph.edges("A1", "A5");
    const [edgeA6A7] = graph.edges("A6", "A7");
    if (!edgeA1A5 || !edgeA6A7) throw new Error("the graph must contain both helper edges");

    // Other edges of the selected edge's endpoints stay visible.
    expect(edgeReducer(edgeA1A5, EDGE_BASE)).toMatchObject({ color: EDGE_BASE.color });
    // And their far ends are not dimmed.
    expect(nodeReducer("A5", NODE_BASE)).toMatchObject({
      color: NODE_BASE.color,
      size: NODE_BASE.size,
    });
    // Unrelated edges are still hidden.
    expect(edgeReducer(edgeA6A7, EDGE_BASE)).toMatchObject({ hidden: true });
  });

  it("edgeReducer hides edges that do not touch the focus, but leaves touching ones alone", async () => {
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
    if (!touchingFocus || !notTouchingFocus) throw new Error("the graph must contain both edges");

    expect(edgeReducer(touchingFocus, EDGE_BASE)).toMatchObject({ color: EDGE_BASE.color });
    expect(edgeReducer(notTouchingFocus, EDGE_BASE)).toMatchObject({ hidden: true }); // hidden, not dimmed
  });

  it("edgeReducer highlights the selected edge regardless of the s/t order", async () => {
    const data = await loadSampleGraphData();
    const graph = new Graph();
    graph.addNode("A1", { x: 0, y: 0 });
    graph.addNode("A2", { x: 1, y: 1 });
    graph.addEdge("A1", "A2", { weight: 3 });
    const store = new Store<AppState>(
      initialState({ selection: { kind: "edge", s: "A2", t: "A1", w: 3 } }),
    );
    const { renderer, getReducer } = fakeRenderer(graph);

    // Edges are visible: the threshold is above the default ratio 1.
    mountReactiveGraph(renderer, store, data, NO_PUB_DETAILS);
    const edgeReducer = getReducer("edgeReducer");
    const [edgeKey] = graph.edges();
    if (!edgeKey) throw new Error("the graph must contain at least one edge");

    expect(edgeReducer(edgeKey, EDGE_BASE)).toMatchObject({
      color: darkTheme.map.edgeSelected,
      size: MAP_CONFIG.edge.widthSelected,
    });
  });

  it("edges between departments are never drawn as a line", async () => {
    const data = await loadSampleGraphData();
    const graph = new Graph();
    graph.addNode(deptNodeKey(0), { x: 0, y: 0, size: 0 });
    graph.addNode(deptNodeKey(1), { x: 1, y: 1, size: 0 });
    graph.addEdge(deptNodeKey(0), deptNodeKey(1), { weight: 2 });
    const store = new Store<AppState>(initialState());
    const { renderer, getReducer } = fakeRenderer(graph);

    // Real edges would be visible at this zoom; department edges still are not.
    mountReactiveGraph(renderer, store, data, NO_PUB_DETAILS);
    const [edgeKey] = graph.edges();
    if (!edgeKey) throw new Error("the graph must contain an edge");

    expect(getReducer("edgeReducer")(edgeKey, EDGE_BASE)).toMatchObject({ hidden: true });
  });
});

describe("applyGraphStyling edge visibility by camera.ratio", () => {
  it("hides edges when ratio is above the threshold (filters.edgeZoomThreshold), shows them when below", async () => {
    const data = await loadSampleGraphData();
    const graph = new Graph();
    graph.addNode("A1", { x: 0, y: 0 });
    graph.addNode("A2", { x: 1, y: 1 });
    graph.addEdge("A1", "A2");
    const edgeZoomThreshold = 0.5;
    const store = new Store<AppState>(
      initialState({ filters: { ...NO_FILTER, edgeZoomThreshold } }),
    );
    const { renderer, getReducer, fireCameraUpdated } = fakeRenderer(graph);

    mountReactiveGraph(renderer, store, data, NO_PUB_DETAILS);
    const edgeReducer = getReducer("edgeReducer");
    const [edgeKey] = graph.edges();
    if (!edgeKey) throw new Error("the graph must contain an edge");

    fireCameraUpdated(edgeZoomThreshold + 0.1);
    expect(edgeReducer(edgeKey, EDGE_BASE)).toMatchObject({ hidden: true });

    fireCameraUpdated(edgeZoomThreshold - 0.1);
    expect(edgeReducer(edgeKey, EDGE_BASE)).not.toMatchObject({ hidden: true });
  });
});

describe("mountZoomDebug", () => {
  it("draws the indicator INSIDE the map container (not document.body), shows camera.ratio, updates, unmount removes the element", () => {
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
  it("does not rebuild the graph on mount (it is already populated outside), rebuilds on tab/lang/filters change, but not on selection change", async () => {
    const data = await loadSampleGraphData();
    const graph = new Graph();
    populateGraph(graph, data, "ru", 1, NO_FILTER, NO_PUB_DETAILS);
    const store = new Store<AppState>(initialState());
    const { renderer, refresh } = fakeRenderer(graph);
    const authorsRealCount = realNodeKeys(graph).length;

    mountReactiveGraph(renderer, store, data, NO_PUB_DETAILS);
    expect(realNodeKeys(graph)).toHaveLength(authorsRealCount);

    store.set({ selection: { kind: "node", key: "A1" } });
    expect(refresh).toHaveBeenCalledTimes(1); // selection only refreshes, no rebuild
    expect(realNodeKeys(graph)).toHaveLength(authorsRealCount); // graph not rebuilt

    store.set({ tab: 2 });
    expect(realNodeKeys(graph)).toHaveLength(data.repos.length); // rebuilt for the new tab
    // A1 does not exist in the repos graph, so the selection is cleared; that causes the second refresh().
    expect(store.get().selection).toBeNull();
    expect(refresh).toHaveBeenCalledTimes(2);

    store.set({ lang: "en" });
    expect(realNodeKeys(graph)).toHaveLength(data.repos.length); // same tab, rebuilt without crashing

    store.set({ filters: { ...store.get().filters, minCoauth: 5 } });
    expect(refresh).toHaveBeenCalledTimes(2); // rebuilds alone never call refresh()
  });

  it("changing a filter that hides the selected publication (filters.yearMax) also clears the stale selection", async () => {
    const data = await loadSampleGraphData();
    const graph = new Graph();
    populateGraph(graph, data, "ru", 3, NO_FILTER, NO_PUB_DETAILS);
    const pub2024 = data.pubs.find((p) => p.year === 2024);
    if (!pub2024) throw new Error("the fixture must contain a 2024 publication");
    const store = new Store<AppState>(
      initialState({ tab: 3, selection: { kind: "node", key: pub2024.key } }),
    );
    const { renderer } = fakeRenderer(graph);

    mountReactiveGraph(renderer, store, data, NO_PUB_DETAILS);
    store.set({ filters: { ...store.get().filters, yearMax: 2022 } }); // hides pub2024

    expect(graph.hasNode(pub2024.key)).toBe(false); // the pub is really gone
    expect(store.get().selection).toBeNull();
  });

  it("selecting a node flies the camera to the coordinates from getNodeDisplayData (framed graph), NOT to raw graph.getNodeAttributes()", async () => {
    const data = await loadSampleGraphData();
    const graph = new Graph();
    graph.addNode("A1", { x: 12, y: 34 });
    const store = new Store<AppState>(initialState());
    const { renderer, cameraAnimate } = fakeRenderer(graph);

    mountReactiveGraph(renderer, store, data, NO_PUB_DETAILS);
    store.set({ selection: { kind: "node", key: "A1" } });

    // Display coordinates are offset from raw ones; raw coordinates sent the camera off screen.
    expect(cameraAnimate).toHaveBeenCalledWith(
      {
        x: 12 + FRAMED_COORD_OFFSET,
        y: 34 + FRAMED_COORD_OFFSET,
        ratio: MAP_CONFIG.camera.focusRatio,
      },
      { duration: MAP_CONFIG.camera.focusDuration, easing: "quadraticInOut" },
    );
  });

  it("changing the tab AND selection in ONE patch (as features/globalSearch.ts does) also flies the camera, not only separate store.set() calls", async () => {
    // Regression: a tab change returned early, so the camera ignored a selection in the same patch.
    const data = await loadSampleGraphData();
    const graph = new Graph();
    populateGraph(graph, data, "ru", 1, NO_FILTER, NO_PUB_DETAILS); // starts on tab 1
    const store = new Store<AppState>(initialState({ tab: 1 }));
    const { renderer, cameraAnimate } = fakeRenderer(graph);
    const repo = data.repos[0];
    if (!repo) throw new Error("the fixture must contain at least one repository");

    mountReactiveGraph(renderer, store, data, NO_PUB_DETAILS);
    store.set({ tab: 2, selection: { kind: "node", key: repo.key } }); // one patch, like global search

    expect(cameraAnimate).toHaveBeenCalledWith(
      {
        x: repo.gx + FRAMED_COORD_OFFSET,
        y: repo.gy + FRAMED_COORD_OFFSET,
        ratio: MAP_CONFIG.camera.focusRatio,
      },
      { duration: MAP_CONFIG.camera.focusDuration, easing: "quadraticInOut" },
    );
  });

  it("selecting a department moves the camera to its anchor without zooming in, otherwise the regions would disappear", async () => {
    const data = await loadSampleGraphData();
    const graph = new Graph();
    graph.addNode(deptNodeKey(0), { x: 5, y: 6, size: 0 });
    const store = new Store<AppState>(initialState());
    const { renderer, cameraAnimate } = fakeRenderer(graph);

    mountReactiveGraph(renderer, store, data, NO_PUB_DETAILS);
    store.set({ selection: { kind: "dept", id: 0 } });

    expect(cameraAnimate).toHaveBeenCalledWith(
      {
        x: 5 + FRAMED_COORD_OFFSET,
        y: 6 + FRAMED_COORD_OFFSET,
      },
      { duration: MAP_CONFIG.camera.focusDuration, easing: "quadraticInOut" },
    );
  });

  it("selecting an edge does not move the camera, an edge has no single point to fly to", async () => {
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

  it("an edge selection survives a graph rebuild (lang change) regardless of the s/t order, graph.hasEdge() is direction-sensitive, the selected edge is not", async () => {
    // mergeEdge on a mixed graph makes a directed edge, so selectionExistsIn() must check both orders.
    const data = await loadSampleGraphData();
    const graph = new Graph();
    populateGraph(graph, data, "ru", 1, NO_FILTER, NO_PUB_DETAILS);
    const edge = data.coauth_edges[0];
    if (!edge) throw new Error("the fixture must contain at least one coauth edge");
    const store = new Store<AppState>(
      initialState({ selection: { kind: "edge", s: edge.t, t: edge.s, w: edge.w } }), // s/t swapped on purpose
    );
    const { renderer } = fakeRenderer(graph);

    mountReactiveGraph(renderer, store, data, NO_PUB_DETAILS);
    store.set({ lang: "en" }); // harmless rebuild that still runs selectionExistsIn()

    expect(store.get().selection).not.toBeNull();
  });
});
