import { describe, expect, it } from "vitest";
import { formatBackupDate } from "../src/features/operations/date";

describe("formatBackupDate", () => {
  it("uses the Moscow calendar day when UTC is still on the previous day", () => {
    const result = formatBackupDate("2026-04-01T21:30:00Z", new Date("2026-04-02T12:00:00Z"));
    expect(result.label).toBe("Сегодня, 00:30");
    expect(result.title).toContain("2026");
    expect(result.title).toContain("00:30:00");
  });

  it("recognises yesterday across Moscow midnight and a month boundary", () => {
    expect(formatBackupDate("2026-04-30T20:59:00Z", new Date("2026-04-30T21:01:00Z")).label).toBe(
      "Вчера, 23:59",
    );
  });

  it("recognises yesterday across a year boundary", () => {
    expect(formatBackupDate("2025-12-31T20:59:00Z", new Date("2025-12-31T21:01:00Z")).label).toBe(
      "Вчера, 23:59",
    );
  });

  it("includes the year for an older date in another year", () => {
    const label = formatBackupDate("2025-12-29T09:00:00Z", new Date("2026-01-02T09:00:00Z")).label;
    expect(label).toContain("29 декабря 2025");
    expect(label).toContain("12:00");
  });
});
