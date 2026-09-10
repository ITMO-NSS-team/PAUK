import { describe, expect, it } from "vitest";
import type { PubDetail, RepoDetail } from "../src/contracts/graph";
import { indexDetailsByKey } from "../src/core/data";
import { buildSearchIndex, deptHitKey, parseDeptHitKey, searchHits } from "../src/features/search";
import { loadSampleGraphData, loadSamplePubDetails, loadSampleRepoDetails } from "./fixtures";

// Логика поиска (эта, чисто функциональная часть) осталась в
// features/search/index.ts, хотя вкладка "Поиск" (features/tabs/search.ts)
// временно убрана из UI — см. память проекта. Тесты на саму вкладку удалены
// вместе с ней, эти — на переиспользуемую основу — остаются.
const NO_PUB_DETAILS = new Map<string, PubDetail>();
const NO_REPO_DETAILS = new Map<string, RepoDetail>();

describe("deptHitKey / parseDeptHitKey", () => {
  it("парсинг возвращает то же число, что было закодировано", () => {
    for (const id of [0, 1, 42]) {
      expect(parseDeptHitKey(deptHitKey(id))).toBe(id);
    }
  });
});

describe("buildSearchIndex", () => {
  it("включает все виды сущностей: авторов, репозитории, публикации, департаменты", async () => {
    const data = await loadSampleGraphData();
    const index = buildSearchIndex(data, "ru", NO_PUB_DETAILS, NO_REPO_DETAILS);

    const total = data.authors.length + data.repos.length + data.pubs.length + data.departments.length;
    expect(index).toHaveLength(total);
    expect(index.some((hit) => hit.kind === "dept")).toBe(true);
  });

  it("для публикаций использует настоящее название и добавляет журнал в sub, когда есть pubDetails", async () => {
    const data = await loadSampleGraphData();
    const pubDetails = indexDetailsByKey(await loadSamplePubDetails());
    const index = buildSearchIndex(data, "ru", pubDetails, NO_REPO_DETAILS);

    for (const pub of data.pubs) {
      const hit = index.find((h) => h.kind === "pub" && h.key === pub.key);
      const detail = pubDetails.get(pub.key);
      expect(hit?.label).toBe(detail?.label);
      expect(hit?.sub).toContain(detail?.journal);
    }
  });

  it("для репозиториев берёт короткий путь на GitHub из repoDetails (url больше не на RepoNode)", async () => {
    const data = await loadSampleGraphData();
    const repoDetails = indexDetailsByKey(await loadSampleRepoDetails());
    const index = buildSearchIndex(data, "ru", NO_PUB_DETAILS, repoDetails);

    for (const repo of data.repos) {
      const hit = index.find((h) => h.kind === "repo" && h.key === repo.key);
      const url = repoDetails.get(repo.key)?.url ?? "";
      expect(hit?.sub).toBe(url.replace("https://github.com/", ""));
    }
  });

  it("для репозитория без записи в repoDetails sub — null, а не падение", async () => {
    const data = await loadSampleGraphData();
    const index = buildSearchIndex(data, "ru", NO_PUB_DETAILS, NO_REPO_DETAILS);

    const hit = index.find((h) => h.kind === "repo");
    expect(hit?.sub).toBeNull();
  });
});

describe("searchHits", () => {
  it("пустой запрос — пустой список результатов, а не всё подряд", async () => {
    const data = await loadSampleGraphData();
    const index = buildSearchIndex(data, "ru", NO_PUB_DETAILS, NO_REPO_DETAILS);
    expect(searchHits(index, "")).toEqual([]);
    expect(searchHits(index, "   ")).toEqual([]);
  });

  it("находит по подстроке в label без учёта регистра", async () => {
    const data = await loadSampleGraphData();
    const index = buildSearchIndex(data, "ru", NO_PUB_DETAILS, NO_REPO_DETAILS);
    const author = data.authors[0];
    if (!author) throw new Error("фикстура должна содержать хотя бы одного автора");

    const hits = searchHits(index, author.label.slice(0, 3).toUpperCase());
    expect(hits.some((hit) => hit.key === author.key)).toBe(true);
  });
});
