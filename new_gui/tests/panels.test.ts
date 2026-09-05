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

  it("показывает карточку «Обзор» со сводными числами, пока ничего не выбрано", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState());
    mountPanel(store, data, NO_SEARCH_DETAILS);

    expect(panel.hidden).toBe(false);
    expect(panel.querySelector("h3")?.textContent).toBe("Обзор");
    expect(panel.textContent).toContain(String(data.authors.length));
    expect(panel.textContent).toContain(String(data.repos.length));
    expect(panel.textContent).toContain(String(data.pubs.length));
    expect(panel.textContent).toContain(String(data.departments.length));
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

  it("карточка автора показывает его публикации и топ соавторов по убыванию веса", async () => {
    const data = await loadSampleGraphData();
    // A1 во фикстуре: публикации P1, P2, P5 (all_edges); соавторы A2 (w=2) и A3 (w=1).
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "node", key: "A1" } });

    mountPanel(store, data, NO_SEARCH_DETAILS);

    expect(panel.textContent).toContain("P1");
    expect(panel.textContent).toContain("P2");
    expect(panel.textContent).toContain("P5");

    const text = panel.textContent ?? "";
    const coauthorsRow = text.indexOf("Топ соавторов");
    expect(coauthorsRow).toBeGreaterThan(-1);
    // A2 (вес 2) должен идти раньше A3 (вес 1) — сортировка по убыванию веса.
    expect(text.indexOf("Петрова А.С.")).toBeGreaterThan(coauthorsRow);
    expect(text.indexOf("Петрова А.С.")).toBeLessThan(text.indexOf("Сидоров П."));
  });

  it("карточка автора показывает его репозитории (repo_author_edges)", async () => {
    const data = await loadSampleGraphData();
    // A1 во фикстуре — maintainer репозитория R1 (repo_author_edges).
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "node", key: "A1" } });

    mountPanel(store, data, NO_SEARCH_DETAILS);

    const r1 = data.repos.find((repo) => repo.key === "R1");
    if (!r1) throw new Error("фикстура должна содержать репозиторий R1");
    expect(panel.textContent).toContain(r1.label);
  });

  it("не показывает строку репозиториев у автора без единого repo_author_edges", async () => {
    const data = await loadSampleGraphData();
    // A2 во фикстуре ни в одном repo_author_edges не участвует.
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "node", key: "A2" } });

    mountPanel(store, data, NO_SEARCH_DETAILS);

    expect(panel.textContent).not.toContain("Репозитории");
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

  it("показывает DOI и ссылку на код как кликабельные <a>, когда есть searchDetails", async () => {
    const data = await loadSampleGraphData();
    const searchDetails = indexSearchDetailsByKey(await loadSampleSearchDetails());
    // P1 во фикстуре — has_code: true, один code_url.
    const detail = searchDetails.get("P1");
    if (!detail?.has_code || detail.code_url.length === 0) {
      throw new Error("фикстура graph-search.sample.json должна содержать P1 с has_code и хотя бы одним code_url");
    }
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "node", key: "P1" } });

    mountPanel(store, data, searchDetails);

    const doiLink = panel.querySelector("a[href^='https://doi.org/']") as HTMLAnchorElement | null;
    expect(doiLink?.textContent).toBe(detail.doi);
    expect(doiLink?.target).toBe("_blank");
    expect(doiLink?.rel).toContain("noopener");

    const codeLink = panel.querySelector(`a[href="${detail.code_url[0]}"]`) as HTMLAnchorElement | null;
    expect(codeLink?.textContent).toBe(detail.code_url[0]?.replace("https://github.com/", ""));
  });

  it("заменяет code_url с небезопасной схемой (javascript:) на about:blank вместо того, чтобы класть её в href", async () => {
    const data = await loadSampleGraphData();
    const pub = data.pubs[0];
    if (!pub) throw new Error("фикстура должна содержать хотя бы одну публикацию");
    const malicious: SearchDetail = {
      key: pub.key,
      label: "Тестовая публикация",
      journal: "",
      doi: "",
      has_code: true,
      code_url: ["javascript:alert(1)"],
    };
    const searchDetails = new Map([[pub.key, malicious]]);
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "node", key: pub.key } });

    mountPanel(store, data, searchDetails);

    const codeLink = panel.querySelector(`dd a`) as HTMLAnchorElement | null;
    expect(codeLink?.getAttribute("href")).toBe("about:blank");
  });

  it("не показывает строку кода, когда has_code === false, но DOI всё равно показывает", async () => {
    const data = await loadSampleGraphData();
    const searchDetails = indexSearchDetailsByKey(await loadSampleSearchDetails());
    // P2 во фикстуре — has_code: false, code_url пуст, но doi есть.
    const detail = searchDetails.get("P2");
    if (!detail || detail.has_code) throw new Error("фикстура должна содержать P2 с has_code: false");
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "node", key: "P2" } });

    mountPanel(store, data, searchDetails);

    expect(panel.querySelector("a[href^='https://doi.org/']")).not.toBeNull();
    expect(panel.textContent).not.toContain("Код");
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

  it("карточка ребра автор-автор показывает список общих публикаций", async () => {
    const data = await loadSampleGraphData();
    // A1-A2 во фикстуре: w=2, и ровно две реально общие публикации (P1, P5) —
    // согласовано с all_edges, а не просто совпадающее число.
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "edge", s: "A1", t: "A2", w: 2 },
    });

    mountPanel(store, data, NO_SEARCH_DETAILS);

    expect(panel.textContent).toContain("Общие публикации");
    expect(panel.textContent).toContain("P1");
    expect(panel.textContent).toContain("P5");
  });

  it("карточка ребра публикация-публикация показывает список общих авторов", async () => {
    const data = await loadSampleGraphData();
    // P1-P2 во фикстуре: w=1, общий автор — A1 (Иванов И.И.).
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "edge", s: "P1", t: "P2", w: 1 },
    });

    mountPanel(store, data, NO_SEARCH_DETAILS);

    expect(panel.textContent).toContain("Общие авторы");
    expect(panel.textContent).toContain("Иванов И.И.");
  });

  it("не показывает строку общих публикаций, когда общих публикаций реально нет", async () => {
    const data = await loadSampleGraphData();
    // A3-A4 во фикстуре: вес есть (соавторство посчитано иначе), но по
    // all_edges общих публикаций нет вообще — строка не должна появляться.
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "edge", s: "A3", t: "A4", w: 1 },
    });

    mountPanel(store, data, NO_SEARCH_DETAILS);

    expect(panel.textContent).not.toContain("Общие публикации");
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

  it("карточка департамента показывает связанные департаменты по убыванию веса (dept_edges)", async () => {
    const data = await loadSampleGraphData();
    // Департамент 0 во фикстуре связан с 1 (w=2) и 2 (w=1) — 1 должен идти первым.
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "dept", id: 0 } });

    mountPanel(store, data, NO_SEARCH_DETAILS);

    const dept1 = data.departments.find((d) => d.id === 1);
    const dept2 = data.departments.find((d) => d.id === 2);
    if (!dept1 || !dept2) throw new Error("фикстура должна содержать департаменты 1 и 2");

    const text = panel.textContent ?? "";
    expect(text).toContain("Связанные департаменты");
    expect(text.indexOf(dept1.name)).toBeGreaterThan(-1);
    expect(text.indexOf(dept1.name)).toBeLessThan(text.indexOf(dept2.name));
  });

  it("возвращается к карточке «Обзор», когда selection сбрасывают в null", async () => {
    const data = await loadSampleGraphData();
    const author = data.authors[0];
    if (!author) throw new Error("фикстура должна содержать хотя бы одного автора");
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "node", key: author.key } });

    mountPanel(store, data, NO_SEARCH_DETAILS);
    expect(panel.querySelector("h3")?.textContent).toBe(author.label);

    store.set({ selection: null });
    expect(panel.hidden).toBe(false);
    expect(panel.querySelector("h3")?.textContent).toBe("Обзор");
  });
});
