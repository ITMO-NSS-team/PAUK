import { describe, expect, it } from "vitest";

// Confirms the Vitest + jsdom + TypeScript pipeline itself is wired
// correctly. Replaced by real unit tests once contracts/core exist.
describe("test environment", () => {
  it("runs in a DOM environment", () => {
    document.body.innerHTML = '<div id="map"></div>';
    expect(document.getElementById("map")).not.toBeNull();
  });
});
