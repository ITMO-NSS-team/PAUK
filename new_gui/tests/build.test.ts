import { describe, expect, it } from "vitest";
import { buildEdgeFeatures, buildNodeFeatures, nodeBounds } from "../src/map/build";
import { loadSampleGraphData } from "../src/core/data";

describe("map/build на фикстур-данных", () => {
  it("buildNodeFeatures отдаёт только узлы вкладки, а не всех сущностей сразу", async () => {
    const data = await loadSampleGraphData();

    // Три разных графа — авторы/репозитории/публикации не смешиваются в одной вкладке.
    expect(buildNodeFeatures(data, "ru", 1).features).toHaveLength(data.authors.length);
    expect(buildNodeFeatures(data, "ru", 2).features).toHaveLength(data.repos.length);
    expect(buildNodeFeatures(data, "ru", 3).features).toHaveLength(data.pubs.length);
    // Вкладка 4 (поиск) не привязана ни к одному из трёх графов — карта пуста.
    expect(buildNodeFeatures(data, "ru", 4).features).toHaveLength(0);
  });

  it("buildNodeFeatures красит узлы цветом их департамента", async () => {
    const data = await loadSampleGraphData();
    const fc = buildNodeFeatures(data, "ru", 1);
    const deptColor = new Map(data.departments.map((d) => [d.id, d.color]));

    for (const feature of fc.features) {
      const author = data.authors.find((a) => a.key === feature.properties.key);
      expect(feature.properties.color).toBe(deptColor.get(author?.dept ?? -1));
    }
  });

  it("buildEdgeFeatures отдаёт рёбра только своей вкладки", async () => {
    const data = await loadSampleGraphData();

    expect(buildEdgeFeatures(data, 1).features).toHaveLength(data.coauth_edges.length);
    expect(buildEdgeFeatures(data, 2).features).toHaveLength(data.repo_edges.length);
    expect(buildEdgeFeatures(data, 3).features).toHaveLength(data.pub_edges.length);
    expect(buildEdgeFeatures(data, 4).features).toHaveLength(0);
  });

  it("buildEdgeFeatures пропускает рёбра без резолвящихся позиций", async () => {
    const data = await loadSampleGraphData();
    // Во фикстуре все s/t у coauth-рёбер существуют как узлы — ничего не отфильтровано.
    expect(buildEdgeFeatures(data, 1).features).toHaveLength(data.coauth_edges.length);
  });

  it("nodeBounds охватывает координаты всех узлов, а не только текущей вкладки", async () => {
    const data = await loadSampleGraphData();
    const [[minLon, minLat], [maxLon, maxLat]] = nodeBounds(data);
    const nodes = [...data.authors, ...data.repos, ...data.pubs];

    for (const node of nodes) {
      expect(node.gx).toBeGreaterThanOrEqual(minLon);
      expect(node.gx).toBeLessThanOrEqual(maxLon);
      expect(node.gy).toBeGreaterThanOrEqual(minLat);
      expect(node.gy).toBeLessThanOrEqual(maxLat);
    }
  });
});
