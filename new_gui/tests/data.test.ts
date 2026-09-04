import { describe, expect, it } from "vitest";
import { assertGraphData, indexByKey, loadSampleGraphData, nodeLabel, parseWrappedJson } from "../src/core/data";

describe("loadSampleGraphData", () => {
  it("загружает фикстуру и проходит проверку формы", async () => {
    const data = await loadSampleGraphData();

    expect(() => assertGraphData(data)).not.toThrow();
    expect(data.authors.length).toBeGreaterThan(0);
    expect(data.departments.length).toBeGreaterThan(0);
    expect(typeof data.authors[0]?.label_en).toBe("string");
  });
});

describe("indexByKey и nodeLabel", () => {
  it("indexByKey находит любой узел (автора, репозиторий, публикацию) по его key", async () => {
    const data = await loadSampleGraphData();
    const index = indexByKey(data);

    for (const node of [...data.authors, ...data.repos, ...data.pubs]) {
      expect(index.get(node.key)).toBe(node);
    }
    expect(index.get("такого-ключа-точно-нет")).toBeUndefined();
  });

  it("nodeLabel берёт label у автора/репозитория и key у публикации (у PubNode label нет)", async () => {
    const data = await loadSampleGraphData();
    const author = data.authors[0];
    const pub = data.pubs[0];
    if (!author || !pub) throw new Error("фикстура должна содержать хотя бы одного автора и одну публикацию");

    expect(nodeLabel(author)).toBe(author.label);
    expect(nodeLabel(pub)).toBe(pub.key);
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
