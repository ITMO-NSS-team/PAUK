import { describe, expect, it } from "vitest";

describe("test environment", () => {
  it("runs in a DOM environment", () => {
    document.body.innerHTML = '<div id="map"></div>';
    expect(document.getElementById("map")).not.toBeNull();
  });
});
