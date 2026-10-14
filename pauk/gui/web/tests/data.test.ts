import { describe, expect, it } from "vitest";
import type { RepoDetail } from "../src/contracts/graph";
import {
  assertGraphData,
  grantIndex,
  toCsv,
  groupIdOf,
  groupsById,
  indexByKey,
  indexDetailsByKey,
  mergeDetailsInto,
  nodeLabel,
} from "../src/core/data";
import {
  loadSampleAuthorDetails,
  loadSampleGraphData,
  loadSamplePubDetails,
  loadSampleRepoDetails,
} from "./fixtures";

describe("loadSampleGraphData", () => {
  it("loads the fixture and passes the shape check", async () => {
    const data = await loadSampleGraphData();

    expect(() => assertGraphData(data)).not.toThrow();
    expect(data.authors.length).toBeGreaterThan(0);
    expect(data.departments.length).toBeGreaterThan(0);
    expect(typeof data.authors[0]?.label_en).toBe("string");
  });
});

describe("indexByKey and nodeLabel", () => {
  it("indexByKey finds any node (author, repository, publication) by its key", async () => {
    const data = await loadSampleGraphData();
    const index = indexByKey(data);

    for (const node of [...data.authors, ...data.repos, ...data.pubs]) {
      expect(index.get(node.key)).toBe(node);
    }
    expect(index.get("такого-ключа-точно-нет")).toBeUndefined();
  });

  it("nodeLabel takes label from an author/repository and key from a publication (PubNode has no label)", async () => {
    const data = await loadSampleGraphData();
    const author = data.authors[0];
    const pub = data.pubs[0];
    if (!author || !pub) throw new Error("the fixture must contain at least one author and one publication");

    expect(nodeLabel(author, "ru")).toBe(author.label);
    expect(nodeLabel(pub, "ru")).toBe(pub.key);
  });
});

describe("loadSampleAuthorDetails / loadSampleRepoDetails / indexDetailsByKey", () => {
  it("every author in graph-data has an entry in authors-detail (even with empty fields)", async () => {
    const data = await loadSampleGraphData();
    const authorDetails = indexDetailsByKey(await loadSampleAuthorDetails());

    for (const author of data.authors) {
      expect(authorDetails.get(author.key)).toBeDefined();
    }
  });

  it("every repository in graph-data has an entry in repos-detail", async () => {
    const data = await loadSampleGraphData();
    const repoDetails = indexDetailsByKey(await loadSampleRepoDetails());

    for (const repo of data.repos) {
      expect(repoDetails.get(repo.key)).toBeDefined();
    }
  });

  it("indexDetailsByKey works with any *Detail kind, not only publications", async () => {
    const repoDetails = indexDetailsByKey(await loadSampleRepoDetails());
    expect(repoDetails.get("R1")?.description).toBe("Инструменты для построения раскладки графа");
    expect(repoDetails.get("такого-ключа-точно-нет")).toBeUndefined();
  });
});

function repoDetailStub(key: string, description: string): RepoDetail {
  return { key, description, url: "", has_readme: false, license: "", contributors: [], owner_type: "" };
}

describe("mergeDetailsInto", () => {
  it("adds entries to the ALREADY EXISTING map by the same reference, does not create a new one", async () => {
    const target = new Map<string, RepoDetail>();
    expect(target.has("R1")).toBe(false);

    const before = target; // mergeDetailsInto must keep the same reference
    mergeDetailsInto(target, await loadSampleRepoDetails());

    expect(target).toBe(before);
    expect(target.get("R1")?.description).toBe("Инструменты для построения раскладки графа");
  });

  it("leaves entries absent from the input list untouched (merges instead of replacing the whole map)", () => {
    const target = new Map<string, RepoDetail>([["custom", repoDetailStub("custom", "уже был до мержа")]]);

    mergeDetailsInto(target, [repoDetailStub("R1", "новый")]);

    expect(target.get("custom")?.description).toBe("уже был до мержа");
    expect(target.get("R1")?.description).toBe("новый");
  });
});

describe("groupIdOf / groupsById", () => {
  it("a repository is coloured by group, other nodes and old data without group by dept", async () => {
    const data = await loadSampleGraphData();
    const [repo] = data.repos;
    const [author] = data.authors;
    if (!repo || !author) throw new Error("the fixture has no repository or author");
    expect(groupIdOf({ ...repo, group: 7 })).toBe(7);
    expect(groupIdOf(repo)).toBe(repo.dept);
    expect(groupIdOf(author)).toBe(author.dept);
  });

  it("one index for departments and repository groups, only departments without repo_groups", async () => {
    const data = await loadSampleGraphData();
    const [dept] = data.departments;
    if (!dept) throw new Error("the fixture has no departments");
    const group = { ...dept, id: 7, kind: "field" as const, name: "Physics", name_en: "Physics" };
    expect(groupsById({ ...data, repo_groups: [group] }).get(7)).toBe(group);
    expect([...groupsById(data).keys()]).toEqual(data.departments.map((d) => d.id));
  });
});

describe("grantIndex", () => {
  it("groups publications by grant_key, the name and funder are the most frequent spellings", async () => {
    const [detail] = await loadSamplePubDetails();
    if (!detail) throw new Error("the fixture has no publication details");
    const rsf = (grantId: string, key: string | null) => ({ funder: "RSF", grant_id: grantId, grant_key: key });
    const pubDetails = indexDetailsByKey([
      { ...detail, key: "P1", funding: [rsf("18-19-00627", "18-19-00627")] },
      { ...detail, key: "P2", funding: [rsf("18-19-00627", "18-19-00627"), rsf("18-19-", null)] },
      { ...detail, key: "P3", funding: [rsf("Grant 18-19-00627", "18-19-00627")] },
    ]);

    const grant = grantIndex(pubDetails).get("18-19-00627");
    expect(grant).toEqual({ key: "18-19-00627", name: "18-19-00627", funder: "RSF", pubs: ["P1", "P2", "P3"] });
    expect(grantIndex(pubDetails).size).toBe(1); // a cut-short number without a key is not a grant
  });

  it("recomputes when details are merged in the background", async () => {
    const [detail] = await loadSamplePubDetails();
    if (!detail) throw new Error("the fixture has no publication details");
    const pubDetails = new Map<string, typeof detail>();
    expect(grantIndex(pubDetails).size).toBe(0);
    mergeDetailsInto(pubDetails, [
      { ...detail, funding: [{ funder: "RSF", grant_id: "17-71-30029", grant_key: "17-71-30029" }] },
    ]);
    expect(grantIndex(pubDetails).get("17-71-30029")?.pubs).toEqual([detail.key]);
  });
});

describe("toCsv", () => {
  it("Excel BOM, quotes and commas are escaped per RFC 4180", () => {
    expect(toCsv([["a", "b"], ['x,"y"', "z"]])).toBe('\uFEFFa,b\r\n"x,""y""",z');
  });
});
