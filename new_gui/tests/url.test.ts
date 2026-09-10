import { describe, expect, it } from "vitest";
import { parseUrlState, serializeUrlState } from "../src/core/url";
import { loadSampleGraphData } from "./fixtures";

describe("serializeUrlState", () => {
  it("screen: 'menu' — только tab=start, tab/selection состояния игнорируются", () => {
    expect(serializeUrlState({ screen: "menu", tab: 2, selection: { kind: "dept", id: 0 } })).toBe("tab=start");
  });

  it("без selection кладёт только tab, слагом, не числом", () => {
    expect(serializeUrlState({ screen: "app", tab: 2, selection: null })).toBe("tab=repos");
  });

  it("узел — tab+sel+key, без веса", () => {
    const params = new URLSearchParams(
      serializeUrlState({ screen: "app", tab: 1, selection: { kind: "node", key: "A1" } }),
    );
    expect(params.get("tab")).toBe("persons");
    expect(params.get("sel")).toBe("node");
    expect(params.get("key")).toBe("A1");
  });

  it("ребро — tab+sel+s+t, вес НЕ кладёт в URL (берётся из data при разборе)", () => {
    const params = new URLSearchParams(
      serializeUrlState({ screen: "app", tab: 1, selection: { kind: "edge", s: "A1", t: "A2", w: 2 } }),
    );
    expect(params.get("sel")).toBe("edge");
    expect(params.get("s")).toBe("A1");
    expect(params.get("t")).toBe("A2");
    expect(params.has("w")).toBe(false);
  });

  it("департамент — tab+sel+id", () => {
    const params = new URLSearchParams(
      serializeUrlState({ screen: "app", tab: 3, selection: { kind: "dept", id: 0 } }),
    );
    expect(params.get("tab")).toBe("pubs");
    expect(params.get("sel")).toBe("dept");
    expect(params.get("id")).toBe("0");
  });
});

describe("parseUrlState", () => {
  it("пустая строка — меню, ничего не выбрано", async () => {
    const data = await loadSampleGraphData();
    expect(parseUrlState("", data)).toEqual({ screen: "menu", tab: 1, selection: null });
  });

  it("tab=start — тоже меню (явная запись, см. features/urlSync.ts)", async () => {
    const data = await loadSampleGraphData();
    expect(parseUrlState("?tab=start", data)).toEqual({ screen: "menu", tab: 1, selection: null });
  });

  it("неизвестный слаг вкладки тоже откатывается на меню — безопаснее показать выбор, чем угадывать по битой ссылке", async () => {
    const data = await loadSampleGraphData();
    expect(parseUrlState("?tab=nope", data)).toEqual({ screen: "menu", tab: 1, selection: null });
    expect(parseUrlState("?tab=9", data)).toEqual({ screen: "menu", tab: 1, selection: null }); // старый числовой формат больше не распознаётся
  });

  it("восстанавливает выбор узла по ключу, который реально есть в data", async () => {
    const data = await loadSampleGraphData();
    const author = data.authors[0];
    if (!author) throw new Error("фикстура должна содержать хотя бы одного автора");

    expect(parseUrlState(`?tab=persons&sel=node&key=${author.key}`, data)).toEqual({
      screen: "app",
      tab: 1,
      selection: { kind: "node", key: author.key },
    });
  });

  it("несуществующий ключ узла (устаревшая/битая ссылка) откатывается на null, а не падает", async () => {
    const data = await loadSampleGraphData();
    expect(parseUrlState("?tab=persons&sel=node&key=NOPE", data)).toEqual({
      screen: "app",
      tab: 1,
      selection: null,
    });
  });

  it("восстанавливает ребро по s/t в исходном порядке и достаёт вес из data (не из URL)", async () => {
    const data = await loadSampleGraphData();
    // A1-A2 во фикстуре: w=2.
    expect(parseUrlState("?tab=persons&sel=edge&s=A1&t=A2", data)).toEqual({
      screen: "app",
      tab: 1,
      selection: { kind: "edge", s: "A1", t: "A2", w: 2 },
    });
  });

  it("восстанавливает ребро и по перевёрнутому s/t — рёбра неориентированы (порядок концов берётся из data, не из URL)", async () => {
    const data = await loadSampleGraphData();
    expect(parseUrlState("?tab=persons&sel=edge&s=A2&t=A1", data)).toEqual({
      screen: "app",
      tab: 1,
      selection: { kind: "edge", s: "A1", t: "A2", w: 2 },
    });
  });

  it("несуществующая пара s/t для ребра откатывается на null", async () => {
    const data = await loadSampleGraphData();
    expect(parseUrlState("?tab=persons&sel=edge&s=A1&t=A99", data)).toEqual({
      screen: "app",
      tab: 1,
      selection: null,
    });
  });

  it("восстанавливает выбор департамента по id, который реально есть в data", async () => {
    const data = await loadSampleGraphData();
    expect(parseUrlState("?tab=pubs&sel=dept&id=0", data)).toEqual({
      screen: "app",
      tab: 3,
      selection: { kind: "dept", id: 0 },
    });
  });

  it("несуществующий id департамента откатывается на null", async () => {
    const data = await loadSampleGraphData();
    expect(parseUrlState("?tab=pubs&sel=dept&id=999", data)).toEqual({ screen: "app", tab: 3, selection: null });
  });

  it("serializeUrlState -> parseUrlState — круговой обход даёт тот же результат", async () => {
    const data = await loadSampleGraphData();
    const author = data.authors[0];
    if (!author) throw new Error("фикстура должна содержать хотя бы одного автора");
    // tab: 1 ("persons"), не 3 — узел должен принадлежать своей вкладке
    // (см. следующий тест), иначе parseUrlState теперь корректно откатит
    // выбор на null.
    const original = {
      screen: "app" as const,
      tab: 1 as const,
      selection: { kind: "node" as const, key: author.key },
    };

    expect(parseUrlState(`?${serializeUrlState(original)}`, data)).toEqual(original);
  });

  it("узел с другой вкладки (несовпадение kind) откатывается на null — ссылка не может подменить сущность", async () => {
    const data = await loadSampleGraphData();
    const author = data.authors[0];
    if (!author) throw new Error("фикстура должна содержать хотя бы одного автора");

    // author.key реально существует в data, но не как публикация — на
    // tab=pubs это должно откатиться на null, а не тихо принять чужую сущность.
    expect(parseUrlState(`?tab=pubs&sel=node&key=${author.key}`, data)).toEqual({
      screen: "app",
      tab: 3,
      selection: null,
    });
  });

  it("ребро с другой вкладки откатывается на null — ищем только среди рёбер своей вкладки", async () => {
    const data = await loadSampleGraphData();
    // A1-A2 — coauth-ребро (авторы), не существует среди pub_edges.
    expect(parseUrlState("?tab=pubs&sel=edge&s=A1&t=A2", data)).toEqual({
      screen: "app",
      tab: 3,
      selection: null,
    });
  });
});
