import { describe, expect, it, vi } from "vitest";
import { renderList, renderListItem } from "../src/core/render";

describe("renderList", () => {
  it("renders one element per item, in the same order", () => {
    const container = document.createElement("div");
    renderList(container, ["a", "b", "c"], (item) => {
      const el = document.createElement("span");
      el.textContent = item;
      return el;
    });

    expect(container.children).toHaveLength(3);
    expect(Array.from(container.children).map((el) => el.textContent)).toEqual(["a", "b", "c"]);
  });

  it("fully replaces the previous content on a repeated call", () => {
    const container = document.createElement("div");
    renderList(container, ["a", "b"], (item) => document.createElement(item === "a" ? "i" : "b"));

    renderList(container, ["x"], () => document.createElement("span"));

    expect(container.children).toHaveLength(1);
    expect(container.firstElementChild?.tagName).toBe("SPAN");
  });
});

describe("renderListItem", () => {
  it("builds a button with label and meta, calls onClick on click", () => {
    const onClick = vi.fn();
    const item = renderListItem({ label: "Заголовок", meta: "42", onClick });

    expect(item.tagName).toBe("BUTTON");
    expect(item.type).toBe("button");
    expect(item.querySelector(".tab-list-item__label")?.textContent).toBe("Заголовок");
    expect(item.querySelector(".tab-list-item__meta")?.textContent).toBe("42");

    item.click();
    expect(onClick).toHaveBeenCalledOnce();
  });

  it("without meta does not add a second span at all", () => {
    const item = renderListItem({ label: "Только заголовок", onClick: vi.fn() });
    expect(item.querySelector(".tab-list-item__meta")).toBeNull();
  });

  it("selected adds the highlight class, dataKind adds the data-kind attribute", () => {
    const selected = renderListItem({ label: "x", selected: true, onClick: vi.fn() });
    expect(selected.classList.contains("tab-list-item--selected")).toBe(true);

    const withKind = renderListItem({ label: "x", dataKind: "dept", onClick: vi.fn() });
    expect(withKind.dataset.kind).toBe("dept");
  });
});
