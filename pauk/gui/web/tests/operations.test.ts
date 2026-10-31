import { afterEach, describe, expect, it, vi } from "vitest";
import { initialState } from "../src/features/operations/data";
import { mountOperations } from "../src/features/operations";
import { renderSite, renderStatus } from "../src/features/operations/view";

afterEach(() => {
  window.dispatchEvent(new Event("pagehide"));
  vi.unstubAllGlobals();
  document.body.innerHTML = "";
});

describe("operations", () => {
  it("starts without synthetic services, backups or releases", () => {
    const state = initialState();
    expect(state.services).toEqual([]);
    expect(state.backups).toEqual([]);
    expect(state.live).toBeNull();
    expect(renderStatus(state)).not.toContain("Все работают");
    expect(renderSite(state)).not.toContain("OSA");
  });

  it("escapes observed service names and failed health details", () => {
    const state = initialState();
    state.services = [
      {
        name: "<script>bad</script>",
        role: "test",
        version: "test",
        cpu: "—",
        memory: "—",
        icon: "database",
        status: "<error>",
        healthy: false,
      },
    ];
    state.checks = [{ label: "Probe", healthy: false, detail: "<failed>" }];
    const html = renderStatus(state);
    expect(html).toContain("&lt;script&gt;bad&lt;/script&gt;");
    expect(html).toContain("&lt;failed&gt;");
    expect(html).not.toContain("Все работают");
  });

  it("loads desktop statuses without credentials or an account menu", async () => {
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => initialState(),
    });
    vi.stubGlobal("fetch", fetch);
    vi.stubGlobal("matchMedia", () => ({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }));
    document.body.innerHTML =
      '<div id="ops-app"></div><dialog id="ops-dialog"></dialog><div id="ops-toast"></div>';
    Object.defineProperty(HTMLDialogElement.prototype, "close", {
      value: vi.fn(),
      configurable: true,
    });
    mountOperations();
    await vi.waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(fetch).toHaveBeenCalledWith(
      "/api/ops/state",
      expect.objectContaining({ credentials: "omit" }),
    );
    expect(document.querySelector('[data-action="account"]')).toBeNull();
  });

  it("does not contact the control plane on unsupported mobile devices", () => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    vi.stubGlobal("matchMedia", () => ({
      matches: true,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }));
    document.body.innerHTML =
      '<div id="ops-app"></div><dialog id="ops-dialog"></dialog><div id="ops-toast"></div>';
    Object.defineProperty(HTMLDialogElement.prototype, "close", {
      value: vi.fn(),
      configurable: true,
    });
    mountOperations();
    expect(fetch).not.toHaveBeenCalled();
    expect(document.getElementById("ops-app")?.hasAttribute("inert")).toBe(true);
  });
});
