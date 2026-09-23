import { describe, expect, it } from "vitest";
import type { RepoDetail } from "../src/contracts/graph";
import {
  assertGraphData,
  groupIdOf,
  groupsById,
  indexByKey,
  indexDetailsByKey,
  mergeDetailsInto,
  nodeLabel,
} from "../src/core/data";
import { loadSampleAuthorDetails, loadSampleGraphData, loadSampleRepoDetails } from "./fixtures";

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
    expect(repoDetails.get("R1")?.description).toBe("Инструменты для построения раскладки графа");
    expect(repoDetails.get("такого-ключа-точно-нет")).toBeUndefined();
  });
});

/** Минимальный валидный RepoDetail для тестов ниже — важно только поле `description`, остальные нужны лишь для типа. */
function repoDetailStub(key: string, description: string): RepoDetail {
  return { key, description, url: "", has_readme: false, license: "", contributors: [], owner_type: "" };
}

describe("mergeDetailsInto", () => {
  it("добавляет записи в УЖЕ СУЩЕСТВУЮЩУЮ карту по той же ссылке, не создаёт новую", async () => {
    const target = new Map<string, RepoDetail>();
    expect(target.has("R1")).toBe(false);

    const before = target; // та же ссылка, что и target — проверяем, что mergeDetailsInto её не подменяет
    mergeDetailsInto(target, await loadSampleRepoDetails());

    expect(target).toBe(before);
    expect(target.get("R1")?.description).toBe("Инструменты для построения раскладки графа");
  });

  it("не трогает записи, которых нет во входном списке (мержит, а не заменяет карту целиком)", () => {
    const target = new Map<string, RepoDetail>([["custom", repoDetailStub("custom", "уже был до мержа")]]);

    mergeDetailsInto(target, [repoDetailStub("R1", "новый")]);

    expect(target.get("custom")?.description).toBe("уже был до мержа");
    expect(target.get("R1")?.description).toBe("новый");
  });
});

describe("groupIdOf / groupsById", () => {
  it("репозиторий красится по group, остальные узлы и старые данные без group — по dept", async () => {
    const data = await loadSampleGraphData();
    const [repo] = data.repos;
    const [author] = data.authors;
    if (!repo || !author) throw new Error("в фикстуре нет репозитория или автора");
    expect(groupIdOf({ ...repo, group: 7 })).toBe(7);
    expect(groupIdOf(repo)).toBe(repo.dept);
    expect(groupIdOf(author)).toBe(author.dept);
  });

  it("один индекс на департаменты и группы репозиториев, без repo_groups — только департаменты", async () => {
    const data = await loadSampleGraphData();
    const [dept] = data.departments;
    if (!dept) throw new Error("в фикстуре нет департаментов");
    const group = { ...dept, id: 7, kind: "field" as const, name: "Physics", name_en: "Physics" };
    expect(groupsById({ ...data, repo_groups: [group] }).get(7)).toBe(group);
    expect([...groupsById(data).keys()]).toEqual(data.departments.map((d) => d.id));
  });
});
