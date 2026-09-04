import { describe, expect, it } from "vitest";
import { renderList } from "../src/core/render";

describe("renderList", () => {
  it("отрисовывает по одному элементу на каждый item, в том же порядке", () => {
    const container = document.createElement("div");
    renderList(container, ["a", "b", "c"], (item) => {
      const el = document.createElement("span");
      el.textContent = item;
      return el;
    });

    expect(container.children).toHaveLength(3);
    expect(Array.from(container.children).map((el) => el.textContent)).toEqual(["a", "b", "c"]);
  });

  it("полностью заменяет предыдущее содержимое при повторном вызове", () => {
    const container = document.createElement("div");
    renderList(container, ["a", "b"], (item) => document.createElement(item === "a" ? "i" : "b"));

    renderList(container, ["x"], () => document.createElement("span"));

    expect(container.children).toHaveLength(1);
    expect(container.firstElementChild?.tagName).toBe("SPAN");
  });
});
