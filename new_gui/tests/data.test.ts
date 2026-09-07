import { describe, expect, it } from "vitest";
import {
  assertGraphData,
  indexByKey,
  indexDetailsByKey,
  loadSampleAuthorDetails,
  loadSampleGraphData,
  loadSampleRepoDetails,
  nodeLabel,
} from "../src/core/data";

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

    expect(nodeLabel(author, "ru")).toBe(author.label);
    expect(nodeLabel(pub, "ru")).toBe(pub.key);
  });
});

describe("loadSampleAuthorDetails / loadSampleRepoDetails / indexDetailsByKey", () => {
  it("у каждого автора из graph-data есть запись в authors-detail (даже с пустыми полями)", async () => {
    const data = await loadSampleGraphData();
    const authorDetails = indexDetailsByKey(await loadSampleAuthorDetails());

    for (const author of data.authors) {
      expect(authorDetails.get(author.key)).toBeDefined();
    }
  });

  it("у каждого репозитория из graph-data есть запись в repos-detail", async () => {
    const data = await loadSampleGraphData();
    const repoDetails = indexDetailsByKey(await loadSampleRepoDetails());

    for (const repo of data.repos) {
      expect(repoDetails.get(repo.key)).toBeDefined();
    }
  });

  it("indexDetailsByKey работает с любым видом *Detail — не только с публикациями", async () => {
    const repoDetails = indexDetailsByKey(await loadSampleRepoDetails());
    expect(repoDetails.get("R1")?.owner).toBe("example-org");
    expect(repoDetails.get("такого-ключа-точно-нет")).toBeUndefined();
  });
});
