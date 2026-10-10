import { describe, expect, it } from "vitest";
import { parseUrlState, serializeUrlState } from "../src/core/url";
import { loadSampleGraphData } from "./fixtures";

describe("serializeUrlState", () => {
  it("screen: 'menu' gives only tab=menu, tab/selection state is ignored", () => {
    expect(serializeUrlState({ screen: "menu", tab: 2, selection: { kind: "dept", id: 0 } })).toBe("tab=menu");
  });

  it("without selection puts only tab, as a slug, not a number", () => {
    expect(serializeUrlState({ screen: "app", tab: 2, selection: null })).toBe("tab=repos");
  });

  it("node gives tab+sel+key, without weight", () => {
    const params = new URLSearchParams(
      serializeUrlState({ screen: "app", tab: 1, selection: { kind: "node", key: "A1" } }),
    );
    expect(params.get("tab")).toBe("persons");
    expect(params.get("sel")).toBe("node");
    expect(params.get("key")).toBe("A1");
  });

  it("edge gives tab+sel+s+t, the weight is NOT put in the URL (taken from data on parse)", () => {
    const params = new URLSearchParams(
      serializeUrlState({ screen: "app", tab: 1, selection: { kind: "edge", s: "A1", t: "A2", w: 2 } }),
    );
    expect(params.get("sel")).toBe("edge");
    expect(params.get("s")).toBe("A1");
    expect(params.get("t")).toBe("A2");
    expect(params.has("w")).toBe(false);
  });

  it("department gives tab+sel+id", () => {
    const params = new URLSearchParams(
      serializeUrlState({ screen: "app", tab: 3, selection: { kind: "dept", id: 0 } }),
    );
    expect(params.get("tab")).toBe("pubs");
    expect(params.get("sel")).toBe("dept");
    expect(params.get("id")).toBe("0");
  });
});

describe("parseUrlState", () => {
  it("an empty string means the menu, nothing is selected", async () => {
    const data = await loadSampleGraphData();
    expect(parseUrlState("", data)).toEqual({ screen: "menu", tab: 1, selection: null });
  });

  it("tab=menu is also the menu (explicit entry, see features/urlSync.ts)", async () => {
    const data = await loadSampleGraphData();
    expect(parseUrlState("?tab=menu", data)).toEqual({ screen: "menu", tab: 1, selection: null });
  });

  it("an unknown tab slug also falls back to the menu, it is safer to show the choice than to guess from a broken link", async () => {
    const data = await loadSampleGraphData();
    expect(parseUrlState("?tab=nope", data)).toEqual({ screen: "menu", tab: 1, selection: null });
    expect(parseUrlState("?tab=9", data)).toEqual({ screen: "menu", tab: 1, selection: null }); // numeric tab ids are not recognized
  });

  it("restores the node selection by a key that really exists in data", async () => {
    const data = await loadSampleGraphData();
    const author = data.authors[0];
    if (!author) throw new Error("the fixture must contain at least one author");

    expect(parseUrlState(`?tab=persons&sel=node&key=${author.key}`, data)).toEqual({
      screen: "app",
      tab: 1,
      selection: { kind: "node", key: author.key },
    });
  });

  it("a non-existent node key (stale/broken link) falls back to null instead of failing", async () => {
    const data = await loadSampleGraphData();
    expect(parseUrlState("?tab=persons&sel=node&key=NOPE", data)).toEqual({
      screen: "app",
      tab: 1,
      selection: null,
    });
  });

  it("restores an edge by s/t in the original order and takes the weight from data (not from the URL)", async () => {
    const data = await loadSampleGraphData();
    // A1-A2 in the fixture: w=2.
    expect(parseUrlState("?tab=persons&sel=edge&s=A1&t=A2", data)).toEqual({
      screen: "app",
      tab: 1,
      selection: { kind: "edge", s: "A1", t: "A2", w: 2 },
    });
  });

  it("restores an edge by reversed s/t too, edges are undirected (the endpoint order comes from data, not from the URL)", async () => {
    const data = await loadSampleGraphData();
    expect(parseUrlState("?tab=persons&sel=edge&s=A2&t=A1", data)).toEqual({
      screen: "app",
      tab: 1,
      selection: { kind: "edge", s: "A1", t: "A2", w: 2 },
    });
  });

  it("a non-existent s/t pair for an edge falls back to null", async () => {
    const data = await loadSampleGraphData();
    expect(parseUrlState("?tab=persons&sel=edge&s=A1&t=A99", data)).toEqual({
      screen: "app",
      tab: 1,
      selection: null,
    });
  });

  it("restores the department selection by an id that really exists in data", async () => {
    const data = await loadSampleGraphData();
    expect(parseUrlState("?tab=pubs&sel=dept&id=0", data)).toEqual({
      screen: "app",
      tab: 3,
      selection: { kind: "dept", id: 0 },
    });
  });

  it("restores the repository group selection (org/field), its id is not from departments", async () => {
    const data = await loadSampleGraphData();
    const [dept] = data.departments;
    if (!dept) throw new Error("the fixture has no departments");
    const withGroups = {
      ...data,
      repo_groups: [{ ...dept, id: 7, kind: "org" as const, name: "aimclub", name_en: "aimclub" }],
    };
    expect(parseUrlState("?tab=repos&sel=dept&id=7", withGroups).selection).toEqual({ kind: "dept", id: 7 });
    expect(parseUrlState("?tab=repos&sel=dept&id=7", data).selection).toBeNull();
  });

  it("a grant selection survives a round trip through the URL", async () => {
    const data = await loadSampleGraphData();
    const state = { screen: "app" as const, tab: 3 as const, selection: { kind: "grant" as const, key: "075-15-2021-1349" } };
    expect(parseUrlState(`?${serializeUrlState(state)}`, data)).toEqual(state);
  });

  it("a non-existent department id falls back to null", async () => {
    const data = await loadSampleGraphData();
    expect(parseUrlState("?tab=pubs&sel=dept&id=999", data)).toEqual({ screen: "app", tab: 3, selection: null });
  });

  it("serializeUrlState -> parseUrlState round trip gives the same result", async () => {
    const data = await loadSampleGraphData();
    const author = data.authors[0];
    if (!author) throw new Error("the fixture must contain at least one author");
    // tab 1, not 3: the node must belong to its tab (see the next test).
    const original = {
      screen: "app" as const,
      tab: 1 as const,
      selection: { kind: "node" as const, key: author.key },
    };

    expect(parseUrlState(`?${serializeUrlState(original)}`, data)).toEqual(original);
  });

  it("a node from another tab (kind mismatch) falls back to null, a link cannot substitute the entity", async () => {
    const data = await loadSampleGraphData();
    const author = data.authors[0];
    if (!author) throw new Error("the fixture must contain at least one author");

    // author.key exists, but not as a pub, so tab=pubs drops it.
    expect(parseUrlState(`?tab=pubs&sel=node&key=${author.key}`, data)).toEqual({
      screen: "app",
      tab: 3,
      selection: null,
    });
  });

  it("an edge from another tab falls back to null, only the edges of its own tab are searched", async () => {
    const data = await loadSampleGraphData();
    // A1-A2 is a co-authorship edge, not among pub_edges.
    expect(parseUrlState("?tab=pubs&sel=edge&s=A1&t=A2", data)).toEqual({
      screen: "app",
      tab: 3,
      selection: null,
    });
  });
});
