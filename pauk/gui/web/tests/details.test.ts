import { afterEach, expect, it, vi } from "vitest";
import { DetailLoader } from "../src/core/details";

afterEach(() => vi.unstubAllGlobals());

it("retains available data after failure and merges a successful retry", async () => {
  const fetch = vi
    .fn()
    .mockResolvedValueOnce({ ok: false, status: 503 })
    .mockResolvedValueOnce({ ok: true, json: async () => [{ key: "new" }] });
  vi.stubGlobal("fetch", fetch);
  const target = new Map([["existing", { key: "existing" }]]);
  const notify = vi.fn();
  const loader = new DetailLoader("/details.json", target, notify);
  await loader.load();
  expect(loader.status).toBe("failed");
  expect([...target.keys()]).toEqual(["existing"]);
  await loader.load();
  expect(loader.status).toBe("loaded");
  expect([...target.keys()]).toEqual(["existing", "new"]);
  expect(notify).toHaveBeenCalledTimes(4);
});

it("deduplicates concurrent retries", async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => [] });
  vi.stubGlobal("fetch", fetch);
  const loader = new DetailLoader("/details.json", new Map(), vi.fn());
  const first = loader.load();
  expect(loader.load()).toBe(first);
  await first;
  expect(fetch).toHaveBeenCalledTimes(1);
});
