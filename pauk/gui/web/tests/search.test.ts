import { describe, expect, it } from "vitest";
import type { PubDetail, RepoDetail } from "../src/contracts/graph";
import { indexDetailsByKey } from "../src/core/data";
import { buildSearchIndex, deptHitKey, parseDeptHitKey, searchHits } from "../src/features/search";
import {
  loadSampleAuthorDetails,
  loadSampleGraphData,
  loadSamplePubDetails,
  loadSampleRepoDetails,
} from "./fixtures";

const NO_PUB_DETAILS = new Map<string, PubDetail>();
const NO_REPO_DETAILS = new Map<string, RepoDetail>();

describe("deptHitKey / parseDeptHitKey", () => {
  it("parsing returns the same number that was encoded", () => {
    for (const id of [0, 1, 42]) {
      expect(parseDeptHitKey(deptHitKey(id))).toBe(id);
    }
  });
});

describe("buildSearchIndex", () => {
  it("includes all entity kinds: authors, repositories, publications, departments", async () => {
    const data = await loadSampleGraphData();
    const index = buildSearchIndex(data, "ru", NO_PUB_DETAILS, NO_REPO_DETAILS);

    const total =
      data.authors.length + data.repos.length + data.pubs.length + data.departments.length;
    expect(index).toHaveLength(total);
    expect(index.some((hit) => hit.kind === "dept")).toBe(true);
  });

  it("for publications uses the real title and adds the journal to sub when pubDetails is present", async () => {
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

  it("for repositories takes the short GitHub path from repoDetails (url is no longer on RepoNode)", async () => {
    const data = await loadSampleGraphData();
    const repoDetails = indexDetailsByKey(await loadSampleRepoDetails());
    const index = buildSearchIndex(data, "ru", NO_PUB_DETAILS, repoDetails);

    for (const repo of data.repos) {
      const hit = index.find((h) => h.kind === "repo" && h.key === repo.key);
      const url = repoDetails.get(repo.key)?.url ?? "";
      expect(hit?.sub).toBe(url.replace("https://github.com/", ""));
    }
  });

  it("for a repository without an entry in repoDetails sub is null, not a crash", async () => {
    const data = await loadSampleGraphData();
    const index = buildSearchIndex(data, "ru", NO_PUB_DETAILS, NO_REPO_DETAILS);

    const hit = index.find((h) => h.kind === "repo");
    expect(hit?.sub).toBeNull();
  });
});

describe("searchHits", () => {
  it("an empty query gives an empty result list, not everything", async () => {
    const data = await loadSampleGraphData();
    const index = buildSearchIndex(data, "ru", NO_PUB_DETAILS, NO_REPO_DETAILS);
    expect(searchHits(index, "")).toEqual([]);
    expect(searchHits(index, "   ")).toEqual([]);
  });

  it("finds by a substring of label, case-insensitively", async () => {
    const data = await loadSampleGraphData();
    const index = buildSearchIndex(data, "ru", NO_PUB_DETAILS, NO_REPO_DETAILS);
    const author = data.authors[0];
    if (!author) throw new Error("the fixture must contain at least one author");

    const hits = searchHits(index, author.label.slice(0, 3).toUpperCase());
    expect(hits.some((hit) => hit.key === author.key)).toBe(true);
  });
});

describe("search by all spellings", () => {
  it("an author shown in Russian is found by Latin spelling via name_en and the variants from authors-detail", async () => {
    const data = await loadSampleGraphData();
    const authorDetails = indexDetailsByKey(await loadSampleAuthorDetails());
    const index = buildSearchIndex(data, "ru", NO_PUB_DETAILS, NO_REPO_DETAILS, authorDetails);

    // A1: name_en "Ivan Ivanov", OpenAlex "Ivanov Ivan", ORCID "I. Ivanov".
    for (const query of ["ivan ivanov", "ivanov ivan", "i. ivanov", "иванов иван иванович"]) {
      expect(searchHits(index, query).map((hit) => hit.key)).toContain("A1");
    }
    // Spellings are searched only; the label stays in the UI language.
    expect(searchHits(index, "ivan ivanov").find((hit) => hit.key === "A1")?.label).not.toContain(
      "Ivan",
    );
  });

  it("without authors-detail (public build) an author is found by the label in the other language", async () => {
    const data = await loadSampleGraphData();
    const index = buildSearchIndex(data, "ru", NO_PUB_DETAILS, NO_REPO_DETAILS);
    const [author] = data.authors;
    if (!author) throw new Error("the fixture must contain an author");
    expect(searchHits(index, author.label_en).map((hit) => hit.key)).toContain(author.key);
  });

  it("a department is found by its English name and by name variants", async () => {
    const sample = await loadSampleGraphData();
    const data = {
      ...sample,
      departments: sample.departments.map((d) =>
        d.id === 0 ? { ...d, name_variants: ["ИПС"] } : d,
      ),
    };
    const index = buildSearchIndex(data, "ru", NO_PUB_DETAILS, NO_REPO_DETAILS);
    const deptKey = deptHitKey(0);
    expect(searchHits(index, "applied systems").map((hit) => hit.key)).toContain(deptKey);
    expect(searchHits(index, "ипс").map((hit) => hit.key)).toContain(deptKey);
  });
});
