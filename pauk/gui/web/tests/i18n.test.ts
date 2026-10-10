import { describe, expect, it } from "vitest";
import { kindLabel, localize, t } from "../src/core/i18n";

describe("localize", () => {
  it("on ru always returns the first argument, even when an en variant exists", () => {
    expect(localize("Иванов", "Ivanov", "ru")).toBe("Иванов");
  });

  it("on en returns the second argument when present", () => {
    expect(localize("Иванов", "Ivanov", "en")).toBe("Ivanov");
  });

  it("on en falls back to the ru variant when there is no en variant (undefined or empty string)", () => {
    expect(localize("Иванов", undefined, "en")).toBe("Иванов");
    expect(localize("Иванов", "", "en")).toBe("Иванов");
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
