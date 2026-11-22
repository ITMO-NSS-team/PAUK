import { describe, expect, it } from "vitest";
import { kindLabel, localize, t } from "../src/core/i18n";

describe("localize", () => {
  it("on ru prefers the nonblank ru variant", () => {
    expect(localize("Иванов", "Ivanov", "ru")).toBe("Иванов");
  });

  it("on en returns the second argument when present", () => {
    expect(localize("Иванов", "Ivanov", "en")).toBe("Ivanov");
  });

  it("on en falls back to the ru variant when there is no en variant (undefined or empty string)", () => {
    expect(localize("Иванов", undefined, "en")).toBe("Иванов");
    expect(localize("Иванов", "", "en")).toBe("Иванов");
  });

  it("on ru falls back to the en variant when ru is empty", () => {
    expect(localize("", "Ivanov", "ru")).toBe("Ivanov");
  });

  it("treats whitespace-only variants as missing in either language", () => {
    expect(localize(" \t\n\u00a0", "Ivanov", "ru")).toBe("Ivanov");
    expect(localize("Иванов", " \t\n\u00a0", "en")).toBe("Иванов");
  });

  it("returns empty when neither language has a nonblank variant", () => {
    for (const lang of ["ru", "en"] as const) {
      expect(localize("", undefined, lang)).toBe("");
      expect(localize("", "", lang)).toBe("");
      expect(localize(" \t", "\n\u00a0", lang)).toBe("");
    }
  });

  it("preserves nonblank values without rewriting them", () => {
    expect(localize(" Иванов ", " Ivanov ", "ru")).toBe(" Иванов ");
    expect(localize(" Иванов ", " Ivanov ", "en")).toBe(" Ivanov ");
    expect(localize("", " Ivanov ", "ru")).toBe(" Ivanov ");
  });

  it("keeps a masked en fallback masked", () => {
    expect(localize("", "Iv… I.", "ru")).toBe("Iv… I.");
  });
});

describe("t", () => {
  it("returns different strings for ru and en for the same key", () => {
    expect(t("tab.authors", "ru")).toBe("Авторы");
    expect(t("tab.authors", "en")).toBe("Authors");
  });
});

describe("kindLabel", () => {
  it("builds the kind.<kind> key and returns the right translation", () => {
    expect(kindLabel("author", "ru")).toBe("Автор");
    expect(kindLabel("dept", "en")).toBe("Department");
  });
});
