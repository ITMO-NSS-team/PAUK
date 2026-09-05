import { describe, expect, it } from "vitest";
import { loadSampleGraphData } from "../src/core/data";
import { parseUrlState, serializeUrlState } from "../src/core/url";

describe("serializeUrlState", () => {
  it("без selection кладёт только tab", () => {
    expect(serializeUrlState({ tab: 2, selection: null })).toBe("tab=2");
  });

  it("узел — tab+sel+key, без веса", () => {
    const params = new URLSearchParams(serializeUrlState({ tab: 1, selection: { kind: "node", key: "A1" } }));
    expect(params.get("tab")).toBe("1");
    expect(params.get("sel")).toBe("node");
    expect(params.get("key")).toBe("A1");
  });

  it("ребро — tab+sel+s+t, вес НЕ кладёт в URL (берётся из data при разборе)", () => {
    const params = new URLSearchParams(
      serializeUrlState({ tab: 1, selection: { kind: "edge", s: "A1", t: "A2", w: 2 } }),
    );
    expect(params.get("sel")).toBe("edge");
    expect(params.get("s")).toBe("A1");
    expect(params.get("t")).toBe("A2");
    expect(params.has("w")).toBe(false);
  });

  it("департамент — tab+sel+id", () => {
    const params = new URLSearchParams(serializeUrlState({ tab: 4, selection: { kind: "dept", id: 0 } }));
    expect(params.get("sel")).toBe("dept");
    expect(params.get("id")).toBe("0");
  });
});

describe("parseUrlState", () => {
  it("пустая строка — вкладка 1, ничего не выбрано", async () => {
    const data = await loadSampleGraphData();
    expect(parseUrlState("", data)).toEqual({ tab: 1, selection: null });
  });

  it("некорректный tab (вне 1-4 или не число) откатывается на 1", async () => {
    const data = await loadSampleGraphData();
    expect(parseUrlState("?tab=9", data).tab).toBe(1);
    expect(parseUrlState("?tab=abc", data).tab).toBe(1);
  });

  it("восстанавливает выбор узла по ключу, который реально есть в data", async () => {
    const data = await loadSampleGraphData();
    const author = data.authors[0];
    if (!author) throw new Error("фикстура должна содержать хотя бы одного автора");

    expect(parseUrlState(`?tab=1&sel=node&key=${author.key}`, data)).toEqual({
      tab: 1,
      selection: { kind: "node", key: author.key },
    });
  });

  it("несуществующий ключ узла (устаревшая/битая ссылка) откатывается на null, а не падает", async () => {
    const data = await loadSampleGraphData();
    expect(parseUrlState("?tab=1&sel=node&key=NOPE", data)).toEqual({ tab: 1, selection: null });
  });

  it("восстанавливает ребро по s/t в исходном порядке и достаёт вес из data (не из URL)", async () => {
    const data = await loadSampleGraphData();
    // A1-A2 во фикстуре: w=2.
    expect(parseUrlState("?tab=1&sel=edge&s=A1&t=A2", data)).toEqual({
      tab: 1,
      selection: { kind: "edge", s: "A1", t: "A2", w: 2 },
    });
  });

  it("восстанавливает ребро и по перевёрнутому s/t — рёбра неориентированы (порядок концов берётся из data, не из URL)", async () => {
    const data = await loadSampleGraphData();
    expect(parseUrlState("?tab=1&sel=edge&s=A2&t=A1", data)).toEqual({
      tab: 1,
      selection: { kind: "edge", s: "A1", t: "A2", w: 2 },
    });
  });

  it("несуществующая пара s/t для ребра откатывается на null", async () => {
    const data = await loadSampleGraphData();
    expect(parseUrlState("?tab=1&sel=edge&s=A1&t=A99", data)).toEqual({ tab: 1, selection: null });
  });

  it("восстанавливает выбор департамента по id, который реально есть в data", async () => {
    const data = await loadSampleGraphData();
    expect(parseUrlState("?tab=4&sel=dept&id=0", data)).toEqual({ tab: 4, selection: { kind: "dept", id: 0 } });
  });

  it("несуществующий id департамента откатывается на null", async () => {
    const data = await loadSampleGraphData();
    expect(parseUrlState("?tab=4&sel=dept&id=999", data)).toEqual({ tab: 4, selection: null });
  });

  it("serializeUrlState -> parseUrlState — круговой обход даёт тот же результат", async () => {
    const data = await loadSampleGraphData();
    const author = data.authors[0];
    if (!author) throw new Error("фикстура должна содержать хотя бы одного автора");
    const original = { tab: 3 as const, selection: { kind: "node" as const, key: author.key } };

    expect(parseUrlState(`?${serializeUrlState(original)}`, data)).toEqual(original);
  });
});
