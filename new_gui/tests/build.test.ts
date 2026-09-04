import { describe, expect, it } from "vitest";
import { buildEdgeFeatures, buildNodeFeatures, nodeBounds } from "../src/map/build";
import { loadSampleGraphData } from "../src/core/data";

describe("map/build на фикстур-данных", () => {
  it("buildNodeFeatures отдаёт по одной точке на каждый узел, окрашенную по департаменту", async () => {
    const data = await loadSampleGraphData();
    const fc = buildNodeFeatures(data, "ru");

    const expectedCount = data.authors.length + data.repos.length + data.pubs.length;
    expect(fc.features).toHaveLength(expectedCount);

    const deptColor = new Map(data.departments.map((d) => [d.id, d.color]));
    for (const feature of fc.features) {
      const node = [...data.authors, ...data.repos, ...data.pubs].find((n) => n.key === feature.properties.key);
      expect(feature.properties.color).toBe(deptColor.get(node?.dept ?? -1));
    }
  });

  it("buildEdgeFeatures пропускает рёбра без резолвящихся позиций", async () => {
    const data = await loadSampleGraphData();
    const fc = buildEdgeFeatures(data);

    // Во фикстуре все s/t у coauth/repo/pub-рёбер существуют как узлы.
    const expectedCount = data.coauth_edges.length + data.repo_edges.length + data.pub_edges.length;
    expect(fc.features).toHaveLength(expectedCount);
  });

  it("nodeBounds охватывает координаты всех узлов", async () => {
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
