import { beforeEach, describe, expect, it } from "vitest";
import type { SearchDetail } from "../src/contracts/search";
import { indexSearchDetailsByKey, loadSampleGraphData, loadSampleSearchDetails } from "../src/core/data";
import { Store, type AppState } from "../src/core/state";
import { mountPanel } from "../src/features/panels";

const NO_SEARCH_DETAILS = new Map<string, SearchDetail>();

function initialState(): AppState {
  return {
    tab: 1,
    lang: "ru",
    selection: null,
    filters: { minCoauth: 1, minSharedAuthors: 1, yearMax: 2026 },
  };
}

describe("mountPanel", () => {
  let panel: HTMLElement;

  beforeEach(() => {
    panel = document.createElement("div");
    panel.id = "panel";
    document.body.appendChild(panel);
    return () => panel.remove();
  });

  it("скрыта, пока ничего не выбрано", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    mountPanel(store, data, NO_SEARCH_DETAILS);

    expect(panel.hidden).toBe(true);
  });

  it("показывает карточку узла с полями, специфичными для автора", async () => {
    const data = await loadSampleGraphData();
    const author = data.authors[0];
    if (!author) throw new Error("фикстура должна содержать хотя бы одного автора");
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "node", key: author.key } });

    mountPanel(store, data, NO_SEARCH_DETAILS);

    expect(panel.hidden).toBe(false);
    expect(panel.querySelector("h3")?.textContent).toBe(author.label);
    expect(panel.textContent).toContain(String(author.pubs_count));
  });

  it("показывает настоящее название публикации из searchDetails, а не её ключ", async () => {
    const data = await loadSampleGraphData();
    const searchDetails = indexSearchDetailsByKey(await loadSampleSearchDetails());
    const pub = data.pubs[0];
    if (!pub) throw new Error("фикстура должна содержать хотя бы одну публикацию");
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "node", key: pub.key } });

    mountPanel(store, data, searchDetails);

    const title = panel.querySelector("h3")?.textContent;
    expect(title).toBe(searchDetails.get(pub.key)?.label);
    expect(title).not.toBe(pub.key);
  });

  it("показывает карточку ребра с обоими концами и весом", async () => {
    const data = await loadSampleGraphData();
    const edge = data.coauth_edges[0];
    if (!edge) throw new Error("фикстура должна содержать хотя бы одно coauth-ребро");
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "edge", s: edge.s, t: edge.t, w: edge.w },
    });

    mountPanel(store, data, NO_SEARCH_DETAILS);

    expect(panel.hidden).toBe(false);
    expect(panel.textContent).toContain(String(edge.w));
  });

  it("показывает карточку департамента со сводными числами", async () => {
    const data = await loadSampleGraphData();
    const dept = data.departments[0];
    if (!dept) throw new Error("фикстура должна содержать хотя бы один департамент");
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "dept", id: dept.id } });

    mountPanel(store, data, NO_SEARCH_DETAILS);

    expect(panel.hidden).toBe(false);
    expect(panel.querySelector("h3")?.textContent).toBe(dept.name);
    expect(panel.textContent).toContain(String(dept.n_authors));
    expect(panel.textContent).toContain(String(dept.n_repos));
  });

  it("скрывается обратно, когда selection сбрасывают в null", async () => {
    const data = await loadSampleGraphData();
    const author = data.authors[0];
    if (!author) throw new Error("фикстура должна содержать хотя бы одного автора");
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "node", key: author.key } });

    mountPanel(store, data, NO_SEARCH_DETAILS);
    expect(panel.hidden).toBe(false);

    store.set({ selection: null });
    expect(panel.hidden).toBe(true);
  });
});
