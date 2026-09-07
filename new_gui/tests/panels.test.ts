import { beforeEach, describe, expect, it } from "vitest";
import type { AuthorDetail, RepoDetail } from "../src/contracts/graph";
import type { SearchDetail } from "../src/contracts/search";
import {
  indexDetailsByKey,
  loadSampleAuthorDetails,
  loadSampleGraphData,
  loadSampleRepoDetails,
  loadSampleSearchDetails,
  mergeDetailsInto,
} from "../src/core/data";
import { Store, type AppState } from "../src/core/state";
import { mountPanel } from "../src/features/panels";

const NO_SEARCH_DETAILS = new Map<string, SearchDetail>();
const NO_AUTHOR_DETAILS = new Map<string, AuthorDetail>();
const NO_REPO_DETAILS = new Map<string, RepoDetail>();

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
    mountPanel(store, data, NO_SEARCH_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

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

    mountPanel(store, data, NO_SEARCH_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    expect(panel.hidden).toBe(false);
    expect(panel.querySelector("h3")?.textContent).toBe(author.label);
    expect(panel.textContent).toContain(String(author.pubs_count));
  });

  it("карточка автора показывает его публикации и топ соавторов по убыванию веса", async () => {
    const data = await loadSampleGraphData();
    // A1 во фикстуре: публикации P1, P2, P5 (all_edges); соавторы A2 (w=2) и A3 (w=1).
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "node", key: "A1" } });

    mountPanel(store, data, NO_SEARCH_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

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

  it("карточка автора показывает GitHub, ORCID и учёную степень, когда они заполнены", async () => {
    const data = await loadSampleGraphData();
    const authorDetails = indexDetailsByKey(await loadSampleAuthorDetails());
    // A1 в authors-detail.sample.json — degree/github/orcid заполнены.
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "node", key: "A1" } });

    mountPanel(store, data, NO_SEARCH_DETAILS, authorDetails, NO_REPO_DETAILS);

    expect(panel.textContent).toContain("к.т.н.");

    const githubLink = panel.querySelector("a[href='https://github.com/ivanov-ii']") as HTMLAnchorElement | null;
    expect(githubLink?.textContent).toBe("ivanov-ii");

    const orcidLink = panel.querySelector("a[href='https://orcid.org/0000-0001-2345-6789']") as HTMLAnchorElement | null;
    expect(orcidLink?.textContent).toBe("0000-0001-2345-6789");
  });

  it("не показывает строки GitHub/ORCID/степени у автора без этих полей", async () => {
    const data = await loadSampleGraphData();
    // A2 в authors-detail.sample.json - запись ЕСТЬ (detail пришёл), но
    // degree/github/orcid в ней пустые строки. Специально не NO_AUTHOR_DETAILS
    // (пустая карта) - та проверяла бы другой сценарий, "detail ещё не
    // пришёл" (индикатор загрузки), а не "поля реально пустые".
    const authorDetails = indexDetailsByKey(await loadSampleAuthorDetails());
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "node", key: "A2" } });

    mountPanel(store, data, NO_SEARCH_DETAILS, authorDetails, NO_REPO_DETAILS);

    expect(panel.querySelector("a[href^='https://github.com/']")).toBeNull();
    expect(panel.querySelector("a[href^='https://orcid.org/']")).toBeNull();
    expect(panel.querySelector(".loading-indicator")).toBeNull();
  });

  it("карточка автора показывает его репозитории (repo_author_edges)", async () => {
    const data = await loadSampleGraphData();
    // A1 во фикстуре — maintainer репозитория R1 (repo_author_edges).
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "node", key: "A1" } });

    mountPanel(store, data, NO_SEARCH_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    const r1 = data.repos.find((repo) => repo.key === "R1");
    if (!r1) throw new Error("фикстура должна содержать репозиторий R1");
    expect(panel.textContent).toContain(r1.label);
  });

  it("не показывает строку репозиториев у автора без единого repo_author_edges", async () => {
    const data = await loadSampleGraphData();
    // A2 во фикстуре ни в одном repo_author_edges не участвует.
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "node", key: "A2" } });

    mountPanel(store, data, NO_SEARCH_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    expect(panel.textContent).not.toContain("Репозитории");
  });

  it("карточка автора показывает варианты имени (name_variants), когда они есть", async () => {
    const data = await loadSampleGraphData();
    const authorDetails = indexDetailsByKey(await loadSampleAuthorDetails());
    // A1 в authors-detail.sample.json — name_variants: ["Ivanov Ivan", "I. Ivanov"].
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "node", key: "A1" } });

    mountPanel(store, data, NO_SEARCH_DETAILS, authorDetails, NO_REPO_DETAILS);

    expect(panel.textContent).toContain("Варианты имени");
    expect(panel.textContent).toContain("Ivanov Ivan");
  });

  it("не показывает строку вариантов имени у автора без name_variants", async () => {
    const data = await loadSampleGraphData();
    // A2 в authors-detail.sample.json - detail пришёл, name_variants: [].
    const authorDetails = indexDetailsByKey(await loadSampleAuthorDetails());
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "node", key: "A2" } });

    mountPanel(store, data, NO_SEARCH_DETAILS, authorDetails, NO_REPO_DETAILS);

    expect(panel.textContent).not.toContain("Варианты имени");
  });

  it("карточка репозитория показывает участников с ролью и публикации репозитория", async () => {
    const data = await loadSampleGraphData();
    // R1 во фикстуре: A1 — maintainer (repo_author_edges), P1 — его публикация (repo_pub_edges).
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "node", key: "R1" } });

    mountPanel(store, data, NO_SEARCH_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    expect(panel.textContent).toContain("Участники");
    expect(panel.textContent).toContain("Иванов И.И. (maintainer)");
    expect(panel.textContent).toContain("P1");
  });

  it("карточка репозитория показывает описание (RepoDetail.description)", async () => {
    const data = await loadSampleGraphData();
    const repoDetails = indexDetailsByKey(await loadSampleRepoDetails());
    const repoDetail = repoDetails.get("R1");
    if (!repoDetail) throw new Error("repos-detail.sample.json должен содержать репозиторий R1");
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "node", key: "R1" } });

    mountPanel(store, data, NO_SEARCH_DETAILS, NO_AUTHOR_DETAILS, repoDetails);

    expect(panel.textContent).toContain(repoDetail.description);
  });

  it("не показывает строки участников/публикаций у репозитория без единой связи", async () => {
    const data = await loadSampleGraphData();
    // R4 во фикстуре не встречается ни в одном repo_pub_edges.
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "node", key: "R4" } });

    mountPanel(store, data, NO_SEARCH_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    expect(panel.textContent).not.toContain("Публикации");
  });

  it("карточка публикации показывает список её авторов (all_edges)", async () => {
    const data = await loadSampleGraphData();
    // P1 во фикстуре: авторы A1 и A2 (all_edges).
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "node", key: "P1" } });

    mountPanel(store, data, NO_SEARCH_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    expect(panel.textContent).toContain("Иванов И.И.");
    expect(panel.textContent).toContain("Петрова А.С.");
  });

  it("показывает настоящее название публикации из searchDetails, а не её ключ", async () => {
    const data = await loadSampleGraphData();
    const searchDetails = indexDetailsByKey(await loadSampleSearchDetails());
    const pub = data.pubs[0];
    if (!pub) throw new Error("фикстура должна содержать хотя бы одну публикацию");
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "node", key: pub.key } });

    mountPanel(store, data, searchDetails, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    const title = panel.querySelector("h3")?.textContent;
    expect(title).toBe(searchDetails.get(pub.key)?.label);
    expect(title).not.toBe(pub.key);
  });

  it("показывает DOI и ссылку на код как кликабельные <a>, когда есть searchDetails и нет связанного репозитория", async () => {
    const data = await loadSampleGraphData();
    const searchDetails = indexDetailsByKey(await loadSampleSearchDetails());
    // P4 во фикстуре — has_code: true, один code_url, и НЕ участвует ни в
    // одном repo_pub_edges (в отличие от P1) — код показываем как ссылку.
    const detail = searchDetails.get("P4");
    if (!detail?.has_code || detail.code_url.length === 0) {
      throw new Error("фикстура graph-search.sample.json должна содержать P4 с has_code и хотя бы одним code_url");
    }
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "node", key: "P4" } });

    mountPanel(store, data, searchDetails, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    const doiLink = panel.querySelector("a[href^='https://doi.org/']") as HTMLAnchorElement | null;
    expect(doiLink?.textContent).toBe(detail.doi);
    expect(doiLink?.target).toBe("_blank");
    expect(doiLink?.rel).toContain("noopener");

    const codeLink = panel.querySelector(`a[href="${detail.code_url[0]}"]`) as HTMLAnchorElement | null;
    expect(codeLink?.textContent).toBe(detail.code_url[0]?.replace("https://github.com/", ""));
  });

  it("показывает ссылку на связанный репозиторий ВМЕСТО code_url, когда публикация связана с репозиторием (repo_pub_edges)", async () => {
    const data = await loadSampleGraphData();
    const searchDetails = indexDetailsByKey(await loadSampleSearchDetails());
    // P1 во фикстуре связана с R1 (repo_pub_edges) и одновременно имеет
    // свой code_url в searchDetails — репозиторий должен победить.
    const detail = searchDetails.get("P1");
    if (!detail?.has_code || detail.code_url.length === 0) {
      throw new Error("фикстура graph-search.sample.json должна содержать P1 с has_code и code_url");
    }
    const repo = data.repos.find((r) => r.key === "R1");
    if (!repo) throw new Error("фикстура должна содержать репозиторий R1");
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "node", key: "P1" } });

    mountPanel(store, data, searchDetails, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    expect(panel.textContent).toContain(repo.label);
    expect(panel.querySelector(`a[href="${detail.code_url[0]}"]`)).toBeNull();
  });

  it("заменяет code_url с небезопасной схемой (javascript:) на about:blank вместо того, чтобы класть её в href", async () => {
    const data = await loadSampleGraphData();
    // P2 — специально не P1: P1 связана с репозиторием через repo_pub_edges,
    // и тогда ссылка на репозиторий заслонила бы собой code_url целиком,
    // а этому тесту нужно, чтобы код реально дошёл до ветки с codeLink().
    const pub = data.pubs.find((p) => p.key === "P2");
    if (!pub) throw new Error("фикстура должна содержать публикацию P2");
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

    mountPanel(store, data, searchDetails, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    const codeLink = panel.querySelector(`dd a`) as HTMLAnchorElement | null;
    expect(codeLink?.getAttribute("href")).toBe("about:blank");
  });

  it("не показывает строку кода, когда has_code === false, но DOI всё равно показывает", async () => {
    const data = await loadSampleGraphData();
    const searchDetails = indexDetailsByKey(await loadSampleSearchDetails());
    // P2 во фикстуре — has_code: false, code_url пуст, но doi есть.
    const detail = searchDetails.get("P2");
    if (!detail || detail.has_code) throw new Error("фикстура должна содержать P2 с has_code: false");
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "node", key: "P2" } });

    mountPanel(store, data, searchDetails, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

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

    mountPanel(store, data, NO_SEARCH_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

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

    mountPanel(store, data, NO_SEARCH_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

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

    mountPanel(store, data, NO_SEARCH_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

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

    mountPanel(store, data, NO_SEARCH_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    expect(panel.textContent).not.toContain("Общие публикации");
  });

  it("показывает карточку департамента со сводными числами", async () => {
    const data = await loadSampleGraphData();
    const dept = data.departments[0];
    if (!dept) throw new Error("фикстура должна содержать хотя бы один департамент");
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "dept", id: dept.id } });

    mountPanel(store, data, NO_SEARCH_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    expect(panel.hidden).toBe(false);
    expect(panel.querySelector("h3")?.textContent).toBe(dept.name);
    expect(panel.textContent).toContain(String(dept.n_authors));
    expect(panel.textContent).toContain(String(dept.n_repos));
  });

  it("карточка департамента показывает связанные департаменты по убыванию веса (dept_edges)", async () => {
    const data = await loadSampleGraphData();
    // Департамент 0 во фикстуре связан с 1 (w=2) и 2 (w=1) — 1 должен идти первым.
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "dept", id: 0 } });

    mountPanel(store, data, NO_SEARCH_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

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

    mountPanel(store, data, NO_SEARCH_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);
    expect(panel.querySelector("h3")?.textContent).toBe(author.label);

    store.set({ selection: null });
    expect(panel.hidden).toBe(false);
    expect(panel.querySelector("h3")?.textContent).toBe("Обзор");
  });

  it("показывает индикатор загрузки, пока authorDetails ещё пуст (файл не домержился)", async () => {
    const data = await loadSampleGraphData();
    const author = data.authors[0];
    if (!author) throw new Error("фикстура должна содержать хотя бы одного автора");
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "node", key: author.key } });

    // Пустая карта - ровно то состояние, в котором app/main.ts передаёт
    // authorDetails фичам ДО того, как пришёл authors-detail.json.
    mountPanel(store, data, NO_SEARCH_DETAILS, new Map(), NO_REPO_DETAILS);

    expect(panel.querySelector(".loading-indicator")).not.toBeNull();
    expect(panel.textContent).toContain("Подробнее");
  });

  it("после того как authorDetails домержился и пришёл store.notify(), индикатор сменяется реальными полями", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "node", key: "A1" } });
    const authorDetails = new Map<string, AuthorDetail>(); // пуст на момент монтирования

    mountPanel(store, data, NO_SEARCH_DETAILS, authorDetails, NO_REPO_DETAILS);
    expect(panel.querySelector(".loading-indicator")).not.toBeNull();

    // Имитация того, что делает app/main.ts, когда приходит authors-detail.json:
    // мержим в ТУ ЖЕ карту (не создаём новую) и зовём notify().
    mergeDetailsInto(authorDetails, await loadSampleAuthorDetails());
    store.notify();

    expect(panel.querySelector(".loading-indicator")).toBeNull();
    expect(panel.textContent).toContain("к.т.н."); // A1.degree из authors-detail.sample.json
  });

  it("показывает индикатор загрузки для репозитория, пока repoDetails ещё пуст", async () => {
    const data = await loadSampleGraphData();
    const repo = data.repos[0];
    if (!repo) throw new Error("фикстура должна содержать хотя бы один репозиторий");
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "node", key: repo.key } });

    mountPanel(store, data, NO_SEARCH_DETAILS, NO_AUTHOR_DETAILS, new Map());

    expect(panel.querySelector(".loading-indicator")).not.toBeNull();
  });
});
