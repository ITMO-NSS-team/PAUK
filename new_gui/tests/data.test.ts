import { describe, expect, it } from "vitest";
import { assertGraphData, loadSampleGraphData, parseWrappedJson } from "../src/core/data";

describe("loadSampleGraphData", () => {
  it("загружает фикстуру и проходит проверку формы", async () => {
    const data = await loadSampleGraphData();

    expect(() => assertGraphData(data)).not.toThrow();
    expect(data.authors.length).toBeGreaterThan(0);
    expect(data.departments.length).toBeGreaterThan(0);
    expect(typeof data.authors[0]?.label_en).toBe("string");
  });
});

describe("parseWrappedJson", () => {
  it("разбирает легаси-формат window.X=... без хвоста", () => {
    const result = parseWrappedJson<{ a: number }>('window.GRAPH={"a":1}', "window.GRAPH=", "");
    expect(result.a).toBe(1);
  });

  it("бросает понятную ошибку на неожиданном формате", () => {
    expect(() => parseWrappedJson("нет такого префикса", "window.GRAPH=", "")).toThrow(/неожиданный формат/);
  });
});
