import { afterEach, describe, expect, it, vi } from "vitest";
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

describe("t names the university the site was built for", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it("ITMO by default", () => {
    expect(t("start.title", "en")).toBe("ITMO co-authorship and open-source code map");
    expect(t("overview.memberAuthors", "ru")).toBe("Из ИТМО");
  });

  it("UFJF when built with VITE_INSTITUTION=ufjf", async () => {
    vi.stubEnv("VITE_INSTITUTION", "ufjf");
    vi.resetModules();
    const i18n = await import("../src/core/i18n");
    expect(i18n.t("start.title", "en")).toBe("UFJF co-authorship and open-source code map");
    expect(i18n.t("overview.memberAuthors", "ru")).toBe("Из UFJF");
  });

  it("an unknown university falls back to ITMO rather than printing a placeholder", async () => {
    vi.stubEnv("VITE_INSTITUTION", "nowhere");
    vi.resetModules();
    const i18n = await import("../src/core/i18n");
    expect(i18n.t("start.badge", "en")).toBe("Open ITMO project");
  });
});

describe("kindLabel", () => {
  it("builds the kind.<kind> key and returns the right translation", () => {
    expect(kindLabel("author", "ru")).toBe("Автор");
    expect(kindLabel("dept", "en")).toBe("Department");
  });
});
