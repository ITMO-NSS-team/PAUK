import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AuthorDetail, PubDetail, RepoDetail } from "../src/contracts/graph";
import { indexDetailsByKey, mergeDetailsInto } from "../src/core/data";
import { Store, type AppState } from "../src/core/state";
import { mountPanel } from "../src/features/panels";
import {
  loadSampleAuthorDetails,
  loadSampleGraphData,
  loadSamplePubDetails,
  loadSampleRepoDetails,
} from "./fixtures";

const NO_PUB_DETAILS = new Map<string, PubDetail>();
const NO_AUTHOR_DETAILS = new Map<string, AuthorDetail>();
const NO_REPO_DETAILS = new Map<string, RepoDetail>();

function initialState(overrides: Partial<AppState> = {}): AppState {
  return {
    screen: "app",
    tab: 1,
    lang: "ru",
    theme: "dark",
    selection: null,
    filters: {
      minCoauth: 1,
      minSharedAuthors: 1,
      yearMax: 2026,
      showNoDeptAuthors: true,
      showNoDeptPubs: true,
      showExternalAuthors: false,
      showIsolatedAuthors: true,
      edgeZoomThreshold: 0.4,
      showRegions: { 1: false, 2: false, 3: false },
      regionZoomThreshold: 0.25,
      regionMinNodes: 10,
    },
    ...overrides,
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

  it("«Обзор» вкладки авторов — число авторов/департаментов, среднее публикаций, заполненность полей — LOADING, пока detail пуст", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState({ tab: 1 }));
    mountPanel(store, data, NO_PUB_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    expect(panel.hidden).toBe(false);
    expect(panel.querySelector("h3")?.textContent).toBe("Обзор");
    expect(panel.textContent).toContain(String(data.authors.length));
    expect(panel.textContent).toContain(String(data.departments.length));
    const avgPubs = data.authors.reduce((sum, a) => sum + a.pubs_count, 0) / data.authors.length;
    expect(panel.textContent).toContain(avgPubs.toFixed(1));
    // authorDetails is empty: ORCID/GitHub/email shares show a loading indicator, not 0/0.
    expect(panel.querySelectorAll(".loading-indicator").length).toBeGreaterThan(0);
  });

  it("«Обзор» вкладки авторов считает всех авторов, с разбивкой ИТМО/внешние, даже когда внешние скрыты фильтром", async () => {
    const sample = await loadSampleGraphData();
    const data = {
      ...sample,
      authors: sample.authors.map((a, i) => (i === 0 ? { ...a, is_itmo: false } : a)),
    };
    const store = new Store<AppState>(initialState({ tab: 1 })); // showExternalAuthors: false
    mountPanel(store, data, NO_PUB_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    const rows = [...panel.querySelectorAll("dt")].map((dt) => [
      dt.textContent,
      dt.nextElementSibling?.textContent,
    ]);
    expect(rows).toContainEqual(["Авторов", String(data.authors.length)]);
    expect(rows).toContainEqual(["Из ИТМО", String(data.authors.length - 1)]);
    expect(rows).toContainEqual(["Внешних", "1"]);
  });

  it("«Обзор» вкладки репозиториев — число репозиториев, доля с README/лицензией", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState({ tab: 2 }));
    mountPanel(store, data, NO_PUB_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    expect(panel.textContent).toContain(String(data.repos.length));
    expect(panel.querySelectorAll(".loading-indicator").length).toBeGreaterThan(0);
  });

  it("«Обзор» вкладки публикаций — число публикаций, доля с известным годом/DOI/аннотацией", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState({ tab: 3 }));
    mountPanel(store, data, NO_PUB_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    expect(panel.textContent).toContain(String(data.pubs.length));
    const withKnownYear = data.pubs.filter((p) => p.year !== null).length;
    const knownYearPercent = Math.round((withKnownYear / data.pubs.length) * 100);
    expect(panel.textContent).toContain(`${knownYearPercent}%`); // known year counts at once, without pubDetails
    expect(panel.querySelectorAll(".loading-indicator").length).toBeGreaterThan(0); // DOI/abstract wait for pubDetails
  });

  it("«Обзор» вкладки авторов — график «Авторы по департаментам», не топ конкретных авторов", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState({ tab: 1 }));
    mountPanel(store, data, NO_PUB_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    const chart = panel.querySelector(".panel-chart");
    if (!chart) throw new Error("карточка «Обзор» должна содержать график");
    expect(chart.querySelector("h4")?.textContent).toBe("Авторы по департаментам");
    // Fixture: A1/A2/A6 dept 0, A3/A4/A8 dept 1, A5/A7 dept 2.
    const rows = [...chart.querySelectorAll(".chart-bar-row")];
    expect(rows).toHaveLength(3);
    const byLabel = new Map(
      rows.map((row) => [
        row.querySelector(".chart-bar-row__label")?.textContent,
        row.querySelector(".chart-bar-row__value")?.textContent,
      ]),
    );
    expect(byLabel.get("Институт прикладных систем")).toBe("3");
    expect(byLabel.get("Лаборатория анализа данных")).toBe("3");
    expect(byLabel.get("Центр робототехники")).toBe("2");
  });

  it("«Обзор» вкладки публикаций — график «Публикации по годам», в хронологическом порядке, без публикаций с неизвестным годом", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState({ tab: 3 }));
    mountPanel(store, data, NO_PUB_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    const chart = panel.querySelector(".panel-chart");
    if (!chart) throw new Error("карточка «Обзор» должна содержать график");
    expect(chart.querySelector("h4")?.textContent).toBe("Публикации по годам");
    // Fixture: 2021x1, 2022x1, 2023x1, 2024x2 (P1, P6); P2 has no year and is left out.
    const labels = [...chart.querySelectorAll(".chart-bar-row__label")].map((el) => el.textContent);
    expect(labels).toEqual(["2021", "2022", "2023", "2024"]); // chronological, not by size
    const values = [...chart.querySelectorAll(".chart-bar-row__value")].map((el) => el.textContent);
    expect(values).toEqual(["1", "1", "1", "2"]);
  });

  it("«Обзор» вкладки репозиториев — график «Репозитории по звёздам» корзинами, не топ конкретных репозиториев", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState({ tab: 2 }));
    mountPanel(store, data, NO_PUB_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    const chart = panel.querySelector(".panel-chart");
    if (!chart) throw new Error("карточка «Обзор» должна содержать график");
    expect(chart.querySelector("h4")?.textContent).toBe("Репозитории по звёздам");
    // Fixture: R4=3, R2=7 -> "1–9" (2); R3=15, R5=21, R1=42 -> "10–99" (3); other buckets empty.
    const byLabel = new Map(
      [...chart.querySelectorAll(".chart-bar-row")].map((row) => [
        row.querySelector(".chart-bar-row__label")?.textContent,
        row.querySelector(".chart-bar-row__value")?.textContent,
      ]),
    );
    expect(byLabel.get("0")).toBe("0");
    expect(byLabel.get("1–9")).toBe("2");
    expect(byLabel.get("10–99")).toBe("3");
    expect(byLabel.get("100–999")).toBe("0");
    expect(byLabel.get("1000+")).toBe("0");
    // Buckets only, no links to individual repos.
    expect(chart.querySelector("button.panel-entity-ref")).toBeNull();
  });

  it("показывает карточку узла с полями, специфичными для автора", async () => {
    const data = await loadSampleGraphData();
    const author = data.authors[0];
    if (!author) throw new Error("фикстура должна содержать хотя бы одного автора");
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: author.key },
    });

    mountPanel(store, data, NO_PUB_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    expect(panel.hidden).toBe(false);
    expect(panel.querySelector("h3")?.textContent).toBe(author.label);
    expect(panel.textContent).toContain(String(author.pubs_count));
  });

  it("карточка автора показывает бейдж «Автор» и кнопку «← Обзор»", async () => {
    const data = await loadSampleGraphData();
    const author = data.authors[0];
    if (!author) throw new Error("фикстура должна содержать хотя бы одного автора");
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: author.key },
    });

    mountPanel(store, data, NO_PUB_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    expect(panel.querySelector(".panel-kind")?.textContent).toBe("Автор");
    expect(panel.querySelector(".panel-back")?.textContent).toBe("← Обзор");
  });

  it("карточка автора после domержа detail показывает имя на ВТОРОМ языке под заголовком (мельче, серым)", async () => {
    const data = await loadSampleGraphData();
    const authorDetails = indexDetailsByKey(await loadSampleAuthorDetails());
    // A1: name_ru "Иванов Иван Иванович", name_en "Ivan Ivanov".
    const store = new Store<AppState>({
      ...initialState({ lang: "ru" }),
      selection: { kind: "node", key: "A1" },
    });

    mountPanel(store, data, NO_PUB_DETAILS, authorDetails, NO_REPO_DETAILS);

    expect(panel.querySelector("h3")?.textContent).toBe("Иванов Иван Иванович"); // primary language (ru) is the title
    expect(panel.querySelector(".panel-card__subtitle")?.textContent).toBe("Ivan Ivanov"); // other language under the title

    store.set({ lang: "en" });
    expect(panel.querySelector("h3")?.textContent).toBe("Ivan Ivanov"); // language switched: title and subtitle swap
    expect(panel.querySelector(".panel-card__subtitle")?.textContent).toBe("Иванов Иван Иванович");
  });

  it("имя на втором языке стоит в шапке сразу под заголовком, а не отдельным блоком после неё", async () => {
    const data = await loadSampleGraphData();
    const authorDetails = indexDetailsByKey(await loadSampleAuthorDetails());
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: "A1" },
    });

    mountPanel(store, data, NO_PUB_DETAILS, authorDetails, NO_REPO_DETAILS);

    const titles = panel.querySelector(".panel-card__head .panel-card__titles");
    expect(titles?.children[0]?.tagName).toBe("H3");
    expect(titles?.children[1]?.className).toBe("panel-card__subtitle");
  });

  it("карточка автора разделена на «Общее», «Приватное» и «Служебное»", async () => {
    const data = await loadSampleGraphData();
    const authorDetails = indexDetailsByKey(await loadSampleAuthorDetails());
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: "A1" },
    });

    mountPanel(store, data, NO_PUB_DETAILS, authorDetails, NO_REPO_DETAILS);

    const sections = new Map(
      [...panel.querySelectorAll(".panel-section")].map((section) => [
        section.querySelector(".panel-section__title")?.textContent,
        section,
      ]),
    );
    expect([...sections.keys()]).toEqual(["Общее", "Приватное", "Служебное"]);

    const general = sections.get("Общее");
    expect(general?.textContent).toContain("Департамент");
    expect(general?.querySelector("a[href='https://openalex.org/A5000000001']")).not.toBeNull();
    expect(general?.textContent).toContain("Топ соавторов");

    const privateSection = sections.get("Приватное");
    expect(privateSection?.querySelector("a[href='mailto:ivanov@example.edu']")).not.toBeNull();
    expect(
      privateSection?.querySelector("a[href='https://orcid.org/0000-0001-2345-6789']"),
    ).not.toBeNull();
    expect(privateSection?.textContent).toContain("к.т.н.");

    const service = sections.get("Служебное");
    expect(service?.textContent).toContain("Ключ");
    expect(service?.textContent).toContain("A1");
    // created_at "2026-08-14T10:23:45.123Z" as an ru-RU date, not the raw ISO string.
    expect(service?.textContent).toContain("14.08.2026");
    expect(service?.textContent).not.toContain("T10:23");
  });

  it("пока authors-detail не пришёл, индикатор загрузки стоит в «Приватном», а служебные ключ/тип видны сразу", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: "A1" },
    });

    mountPanel(store, data, NO_PUB_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    const titles = [...panel.querySelectorAll(".panel-section__title")].map((el) => el.textContent);
    expect(titles).toEqual(["Общее", "Приватное", "Служебное"]);
    const privateSection = panel.querySelectorAll(".panel-section")[1];
    expect(privateSection?.querySelector(".loading-indicator")).not.toBeNull();
    expect(panel.querySelectorAll(".panel-section")[2]?.textContent).toContain("A1");
  });

  it("аффилиации с одним названием склеиваются в один пункт: ссылка на ROR, диапазон лет и источники", async () => {
    const data = await loadSampleGraphData();
    const authorDetails = indexDetailsByKey(await loadSampleAuthorDetails());
    // A1: "Sample University" from OpenAlex (2023, 2024) and ORCID (2021); "Other Institute" has no ROR or years.
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: "A1" },
    });

    mountPanel(store, data, NO_PUB_DETAILS, authorDetails, NO_REPO_DETAILS);

    const dt = [...panel.querySelectorAll("dt")].find((el) => el.textContent === "Аффилиации");
    const items = [...(dt?.nextElementSibling?.querySelectorAll("li") ?? [])];
    expect(items.map((li) => li.textContent)).toEqual([
      "Sample University 2021–2024 · OpenAlex, ORCID",
      "Other Institute OpenAlex",
    ]);
    expect(items[0]?.querySelector("a")?.getAttribute("href")).toBe("https://ror.org/0sample01");
    // No ROR: not a link, the source is still a grey suffix.
    expect(items[1]?.querySelector("a")).toBeNull();
    expect(items[1]?.querySelector(".panel-list__meta")?.textContent).toBe("OpenAlex");
  });

  it("публикации автора — по одной на строку, с позицией автора и отметкой «автор для переписки»", async () => {
    const data = await loadSampleGraphData();
    const authorDetails = indexDetailsByKey(await loadSampleAuthorDetails());
    // A1: P1 position 1, corresponding; P2 position 3; P5 has no role.
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: "A1" },
    });

    mountPanel(store, data, NO_PUB_DETAILS, authorDetails, NO_REPO_DETAILS);

    const dt = [...panel.querySelectorAll("dt")].find((el) => el.textContent === "Публикации");
    expect(dt?.classList.contains("panel-row--block")).toBe(true);
    const byPub = new Map(
      [...(dt?.nextElementSibling?.querySelectorAll("li") ?? [])].map((li) => [
        li.querySelector("button.panel-entity-ref")?.textContent,
        li.querySelector(".panel-list__meta")?.textContent ?? null,
      ]),
    );
    expect(byPub.get("P1")).toBe("1-й автор · автор для переписки");
    expect(byPub.get("P2")).toBe("3-й автор");
    expect(byPub.get("P5")).toBeNull();
  });

  it("карточка автора не падает на authors-detail.json, сгенерированном до появления pub_roles/created_at/updated_at", async () => {
    const data = await loadSampleGraphData();
    const [a1] = await loadSampleAuthorDetails();
    if (!a1) throw new Error("фикстура должна содержать автора A1");
    const staleA1: AuthorDetail = { ...a1 };
    delete staleA1.pub_roles;
    delete staleA1.created_at;
    delete staleA1.updated_at;
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: "A1" },
    });

    mountPanel(store, data, NO_PUB_DETAILS, indexDetailsByKey([staleA1]), NO_REPO_DETAILS);

    expect(panel.querySelector("h3")?.textContent).toBe("Иванов Иван Иванович");
    const pubsDt = [...panel.querySelectorAll("dt")].find((el) => el.textContent === "Публикации");
    const pubsList = pubsDt?.nextElementSibling;
    expect(pubsList?.textContent).toContain("P1");
    expect(pubsList?.querySelector(".panel-list__meta")).toBeNull();
    expect(panel.textContent).not.toContain("Создан");
  });

  it("длинный список: первые 3, «+ ещё» добавляет по 20, «свернуть» на любом шаге возвращает 3", async () => {
    const data = await loadSampleGraphData();
    const [a1] = await loadSampleAuthorDetails();
    if (!a1) throw new Error("фикстура должна содержать автора A1");
    const variants = Array.from({ length: 50 }, (_, i) => `Variant ${i + 1}`);
    const authorDetails = indexDetailsByKey([
      { ...a1, name_variants: { openalex: variants, orcid: [] } },
    ]);
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: "A1" },
    });

    mountPanel(store, data, NO_PUB_DETAILS, authorDetails, NO_REPO_DETAILS);

    const dt = [...panel.querySelectorAll("dt")].find(
      (el) => el.textContent === "Варианты написания (OpenAlex)",
    );
    const list = dt?.nextElementSibling?.querySelector(".panel-list");
    const buttons = () => [...(list?.querySelectorAll<HTMLButtonElement>(".panel-list__more") ?? [])];
    const labels = () => buttons().map((b) => b.textContent);
    const count = () => list?.querySelectorAll("li:not(.panel-list__toggle)").length;

    expect(count()).toBe(3);
    expect(labels()).toEqual(["+ ещё 20"]);

    buttons()[0]?.click();
    expect(count()).toBe(23);
    expect(labels()).toEqual(["+ ещё 20", "− свернуть"]);

    buttons()[0]?.click();
    expect(count()).toBe(43);
    expect(labels()).toEqual(["+ ещё 7", "− свернуть"]);

    buttons()[0]?.click();
    expect(count()).toBe(50);
    expect(labels()).toEqual(["− свернуть"]);

    buttons()[0]?.click();
    expect(count()).toBe(3);
    expect(labels()).toEqual(["+ ещё 20"]);

    buttons()[0]?.click();
    buttons()[1]?.click(); // collapse from a middle step
    expect(count()).toBe(3);
  });

  it("карточка автора БЕЗ domержённого detail (только сокращённая подпись узла) не показывает подзаголовок", async () => {
    const data = await loadSampleGraphData();
    const author = data.authors[0];
    if (!author) throw new Error("фикстура должна содержать хотя бы одного автора");
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: author.key },
    });

    mountPanel(store, data, NO_PUB_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    expect(panel.querySelector(".panel-card__subtitle")).toBeNull();
  });

  it("клик по «← Обзор» сбрасывает selection и возвращает карточку «Обзор»", async () => {
    const data = await loadSampleGraphData();
    const author = data.authors[0];
    if (!author) throw new Error("фикстура должна содержать хотя бы одного автора");
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: author.key },
    });

    mountPanel(store, data, NO_PUB_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);
    const back = panel.querySelector<HTMLButtonElement>(".panel-back");
    if (!back) throw new Error("на карточке автора должна быть кнопка «← Обзор»");

    back.click();

    expect(store.get().selection).toBeNull();
    expect(panel.querySelector("h3")?.textContent).toBe("Обзор");
  });

  it("«Обзор» показывает бейдж текущей вкладки, но НЕ показывает кнопку «← Обзор» — возвращаться уже некуда", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>(initialState()); // selection: null, tab: 1

    mountPanel(store, data, NO_PUB_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    expect(panel.querySelector(".panel-kind")?.textContent).toBe("Авторы");
    expect(panel.querySelector(".panel-back")).toBeNull();
  });

  it("карточка автора показывает его публикации и топ соавторов по убыванию веса", async () => {
    const data = await loadSampleGraphData();
    // A1: pubs P1, P2, P5; co-authors A2 (w=2) and A3 (w=1).
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: "A1" },
    });

    mountPanel(store, data, NO_PUB_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    expect(panel.textContent).toContain("P1");
    expect(panel.textContent).toContain("P2");
    expect(panel.textContent).toContain("P5");

    const text = panel.textContent ?? "";
    const coauthorsRow = text.indexOf("Топ соавторов");
    expect(coauthorsRow).toBeGreaterThan(-1);
    // Sorted by weight, descending.
    expect(text.indexOf("Петрова А.С.")).toBeGreaterThan(coauthorsRow);
    expect(text.indexOf("Петрова А.С.")).toBeLessThan(text.indexOf("Сидоров П."));
  });

  it("клик по соавтору в карточке автора делает его новым selection — 'прослеживать связи' одним кликом", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: "A1" },
    });

    mountPanel(store, data, NO_PUB_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    const coauthorButton = [...panel.querySelectorAll("button.panel-entity-ref")].find(
      (button) => button.textContent === "Петрова А.С.",
    ) as HTMLButtonElement | undefined;
    if (!coauthorButton)
      throw new Error("кнопка-ссылка на соавтора А2 (Петрова А.С.) должна быть в карточке");

    coauthorButton.click();

    expect(store.get().selection).toEqual({ kind: "node", key: "A2" });
    expect(store.get().tab).toBe(1); // same kind: the tab stays
  });

  it("клик по публикации в карточке автора делает её новым selection И переключает вкладку на 'Публикации'", async () => {
    // Regression: a pub is another kind than the current (authors) tab. Without
    // switching the tab the camera cannot find P1 in the graph.
    const data = await loadSampleGraphData();
    const store = new Store<AppState>({
      ...initialState({ tab: 1 }),
      selection: { kind: "node", key: "A1" },
    });

    mountPanel(store, data, NO_PUB_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    const pubButton = [...panel.querySelectorAll("button.panel-entity-ref")].find(
      (button) => button.textContent === "P1",
    ) as HTMLButtonElement | undefined;
    if (!pubButton) throw new Error("кнопка-ссылка на публикацию P1 должна быть в карточке");

    pubButton.click();

    expect(store.get().selection).toEqual({ kind: "node", key: "P1" });
    expect(store.get().tab).toBe(3);
  });

  it("внешние ссылки (GitHub/ORCID) остаются <a>, не кнопками — открываются в новой вкладке, а не меняют selection", async () => {
    const data = await loadSampleGraphData();
    const authorDetails = indexDetailsByKey(await loadSampleAuthorDetails());
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: "A1" },
    });

    mountPanel(store, data, NO_PUB_DETAILS, authorDetails, NO_REPO_DETAILS);

    const githubLink = panel.querySelector("a[href='https://github.com/ivanov-ii']");
    expect(githubLink?.tagName).toBe("A");
    expect(githubLink?.getAttribute("target")).toBe("_blank");
  });

  it("карточка автора показывает GitHub, ORCID и учёную степень, когда они заполнены", async () => {
    const data = await loadSampleGraphData();
    const authorDetails = indexDetailsByKey(await loadSampleAuthorDetails());
    // A1: degree, github and orcid are filled.
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: "A1" },
    });

    mountPanel(store, data, NO_PUB_DETAILS, authorDetails, NO_REPO_DETAILS);

    expect(panel.textContent).toContain("к.т.н.");

    const githubLink = panel.querySelector(
      "a[href='https://github.com/ivanov-ii']",
    ) as HTMLAnchorElement | null;
    expect(githubLink?.textContent).toBe("ivanov-ii");

    const orcidLink = panel.querySelector(
      "a[href='https://orcid.org/0000-0001-2345-6789']",
    ) as HTMLAnchorElement | null;
    expect(orcidLink?.textContent).toBe("0000-0001-2345-6789");
  });

  it("карточка автора показывает OpenAlex/Google Scholar/email/аффилиации, когда они заполнены", async () => {
    const data = await loadSampleGraphData();
    const authorDetails = indexDetailsByKey(await loadSampleAuthorDetails());
    // A1: all these fields are filled.
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: "A1" },
    });

    mountPanel(store, data, NO_PUB_DETAILS, authorDetails, NO_REPO_DETAILS);

    const openalexLink = panel.querySelector(
      "a[href='https://openalex.org/A5000000001']",
    ) as HTMLAnchorElement | null;
    expect(openalexLink?.textContent).toBe("A5000000001");

    const scholarLink = panel.querySelector(
      "a[href='https://scholar.google.com/citations?user=sample1']",
    ) as HTMLAnchorElement | null;
    expect(scholarLink?.textContent).toBe("Google Scholar");

    const mailLinks = [...panel.querySelectorAll("a[href^='mailto:']")] as HTMLAnchorElement[];
    expect(mailLinks.map((a) => a.textContent)).toEqual(["ivanov@example.edu"]);

    expect(panel.textContent).toContain("Sample University");
  });

  it("не показывает строки GitHub/ORCID/степени у автора без этих полей", async () => {
    const data = await loadSampleGraphData();
    // A2 has a record with empty degree/github/orcid: "fields are empty", not
    // "detail has not arrived".
    const authorDetails = indexDetailsByKey(await loadSampleAuthorDetails());
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: "A2" },
    });

    mountPanel(store, data, NO_PUB_DETAILS, authorDetails, NO_REPO_DETAILS);

    expect(panel.querySelector("a[href^='https://github.com/']")).toBeNull();
    expect(panel.querySelector("a[href^='https://orcid.org/']")).toBeNull();
    expect(panel.querySelector(".loading-indicator")).toBeNull();
  });

  it("карточка автора показывает его репозитории (repo_author_edges)", async () => {
    const data = await loadSampleGraphData();
    // A1 maintains R1 (repo_author_edges).
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: "A1" },
    });

    mountPanel(store, data, NO_PUB_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    const r1 = data.repos.find((repo) => repo.key === "R1");
    if (!r1) throw new Error("фикстура должна содержать репозиторий R1");
    expect(panel.textContent).toContain(r1.label);
  });

  it("не показывает строку репозиториев у автора без единого repo_author_edges", async () => {
    const data = await loadSampleGraphData();
    // A2 is in no repo_author_edges.
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: "A2" },
    });

    mountPanel(store, data, NO_PUB_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    expect(panel.textContent).not.toContain("Репозитории");
  });

  it("карточка автора показывает варианты имени раздельно по источнику (OpenAlex/ORCID), когда они есть", async () => {
    const data = await loadSampleGraphData();
    const authorDetails = indexDetailsByKey(await loadSampleAuthorDetails());
    // A1: openalex ["Ivanov Ivan"], orcid ["I. Ivanov"].
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: "A1" },
    });

    mountPanel(store, data, NO_PUB_DETAILS, authorDetails, NO_REPO_DETAILS);

    expect(panel.textContent).toContain("Варианты написания (OpenAlex)");
    expect(panel.textContent).toContain("Ivanov Ivan");
    expect(panel.textContent).toContain("Варианты написания (ORCID)");
    expect(panel.textContent).toContain("I. Ivanov");
  });

  it("не показывает строки вариантов имени у автора без name_variants ни по одному источнику", async () => {
    const data = await loadSampleGraphData();
    // A2: detail arrived, openalex/orcid are empty.
    const authorDetails = indexDetailsByKey(await loadSampleAuthorDetails());
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: "A2" },
    });

    mountPanel(store, data, NO_PUB_DETAILS, authorDetails, NO_REPO_DETAILS);

    expect(panel.textContent).not.toContain("Варианты написания");
  });

  it("заголовок карточки автора — полное имя (name_ru/name_en) на текущем языке, как только detail пришёл", async () => {
    const data = await loadSampleGraphData();
    const authorDetails = indexDetailsByKey(await loadSampleAuthorDetails());
    // A1: name_ru "Иванов Иван Иванович", name_en "Ivan Ivanov".
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: "A1" },
    });

    mountPanel(store, data, NO_PUB_DETAILS, authorDetails, NO_REPO_DETAILS);
    expect(panel.querySelector("h3")?.textContent).toBe("Иванов Иван Иванович");

    store.set({ lang: "en" });
    expect(panel.querySelector("h3")?.textContent).toBe("Ivan Ivanov");
  });

  it("заголовок карточки автора остаётся сокращённой подписью, пока detail не пришёл", async () => {
    const data = await loadSampleGraphData();
    const author = data.authors.find((a) => a.key === "A1");
    if (!author) throw new Error("фикстура должна содержать автора A1");
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: "A1" },
    });

    mountPanel(store, data, NO_PUB_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    expect(panel.querySelector("h3")?.textContent).toBe(author.label);
  });

  it("заголовок карточки автора остаётся сокращённой подписью, если полное имя пустое, даже когда detail уже пришёл", async () => {
    const data = await loadSampleGraphData();
    const authorDetails = indexDetailsByKey(await loadSampleAuthorDetails());
    // A2: detail arrived, name_ru/name_en are empty.
    const author2 = data.authors.find((a) => a.key === "A2");
    if (!author2) throw new Error("фикстура должна содержать автора A2");
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: "A2" },
    });

    mountPanel(store, data, NO_PUB_DETAILS, authorDetails, NO_REPO_DETAILS);

    expect(panel.querySelector("h3")?.textContent).toBe(author2.label);
  });

  it("карточка репозитория показывает участников с ролью и публикации репозитория", async () => {
    const data = await loadSampleGraphData();
    // R1: A1 is maintainer (repo_author_edges), P1 is its pub (repo_pub_edges).
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: "R1" },
    });

    mountPanel(store, data, NO_PUB_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    expect(panel.textContent).toContain("Участники");
    expect(panel.textContent).toContain("Иванов И.И. (maintainer)");
    expect(panel.textContent).toContain("P1");
    // Contributors and pubs as lists, one item per line.
    for (const label of ["Участники", "Публикации"]) {
      const dt = [...panel.querySelectorAll("dt")].find((el) => el.textContent === label);
      expect(dt?.nextElementSibling?.querySelector(".panel-list")).not.toBeNull();
    }
  });

  describe("implementation rate пары публикация–репозиторий", () => {
    afterEach(() => {
      vi.unstubAllGlobals();
    });

    it("сначала из чего собран, потом процент — в карточке репозитория и публикации", async () => {
      const data = await loadSampleGraphData();
      // R1 implements P1 (repo_pub_edges).
      const rates = [{ pub: "P1", repo: "R1", implemented: 32, total: 57, pct: 56 }];
      vi.stubGlobal(
        "fetch",
        vi.fn((url: string) =>
          Promise.resolve(
            url.includes("implementation-rates")
              ? new Response(JSON.stringify(rates))
              : new Response(null, { status: 404 }),
          ),
        ),
      );
      const store = new Store<AppState>({
        ...initialState(),
        selection: { kind: "node", key: "R1" },
      });
      mountPanel(store, data, NO_PUB_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);
      await vi.waitFor(() => expect(panel.textContent).toContain("реализовано 32/57 · 56%"));

      store.set({ selection: { kind: "node", key: "P1" } });
      expect(panel.textContent).toContain("реализовано 32/57 · 56%");
    });
  });

  it("длинная аннотация — первые N слов и «читать полностью», по клику — целиком, повторный клик — снова коротко", async () => {
    const data = await loadSampleGraphData();
    const [detail] = await loadSamplePubDetails();
    if (!detail) throw new Error("в фикстуре нет деталей публикации");
    const words = Array.from({ length: 80 }, (_, i) => `w${i + 1}`);
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "node", key: detail.key } });
    mountPanel(store, data, indexDetailsByKey([{ ...detail, abstract: words.join(" ") }]), NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    const dd = [...panel.querySelectorAll("dt")].find((el) => el.textContent === "Аннотация")?.nextElementSibling;
    const toggle = () => dd?.querySelector<HTMLButtonElement>(".panel-list__more");
    expect(dd?.textContent).toContain("w50…");
    expect(dd?.textContent).not.toContain("w51");
    expect(toggle()?.textContent).toBe("+ читать полностью");

    toggle()?.click();
    expect(dd?.textContent).toContain("w80");
    expect(toggle()?.textContent).toBe("− свернуть");

    toggle()?.click();
    expect(dd?.textContent).not.toContain("w51");
  });

  it("короткая аннотация — целиком и без кнопки", async () => {
    const data = await loadSampleGraphData();
    const [detail] = await loadSamplePubDetails();
    if (!detail) throw new Error("в фикстуре нет деталей публикации");
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "node", key: detail.key } });
    mountPanel(store, data, indexDetailsByKey([{ ...detail, abstract: "Short abstract." }]), NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    const dd = [...panel.querySelectorAll("dt")].find((el) => el.textContent === "Аннотация")?.nextElementSibling;
    expect(dd?.textContent).toBe("Short abstract.");
    expect(dd?.querySelector(".panel-list__more")).toBeNull();
  });

  it("карточка репозитория ведёт на GitHub, карточка группы-организации — на профиль организации", async () => {
    const data = await loadSampleGraphData();
    const [dept] = data.departments;
    if (!dept) throw new Error("в фикстуре нет департаментов");
    const org = { ...dept, id: 99, kind: "org" as const, name: "example-org", name_en: "example-org" };
    const field = { ...dept, id: 98, kind: "field" as const, name: "Physics", name_en: "Physics" };
    const repoDetails = indexDetailsByKey(await loadSampleRepoDetails());
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "node", key: "R1" } });
    mountPanel(store, { ...data, repo_groups: [org, field] }, NO_PUB_DETAILS, NO_AUTHOR_DETAILS, repoDetails);

    const repoLink = panel.querySelector<HTMLAnchorElement>("a[href='https://github.com/example-org/graph-toolkit']");
    expect(repoLink?.textContent).toBe("example-org/graph-toolkit");

    store.set({ selection: { kind: "dept", id: 99 } });
    expect(panel.querySelector("a[href='https://github.com/example-org']")?.textContent).toBe("example-org");

    // A field group has no GitHub page.
    store.set({ selection: { kind: "dept", id: 98 } });
    expect(panel.querySelector("a[href^='https://github.com']")).toBeNull();
  });

  it("карточки публикации и репозитория: разделы «Общее»/«Служебное», авторы списком, OpenAlex сразу после DOI", async () => {
    const data = await loadSampleGraphData();
    const pubDetails = indexDetailsByKey(await loadSamplePubDetails());
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "node", key: "P1" } });
    mountPanel(store, data, pubDetails, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    const sectionTitles = () =>
      [...panel.querySelectorAll(".panel-section__title")].map((el) => el.textContent);
    const labels = () => [...panel.querySelectorAll("dt")].map((el) => el.textContent);
    expect(sectionTitles()).toEqual(["Общее", "Служебное"]);
    expect(labels().indexOf("OpenAlex")).toBe(labels().indexOf("DOI") + 1);
    const authorsDd = [...panel.querySelectorAll("dt")].find((el) => el.textContent === "Авторы")
      ?.nextElementSibling;
    expect(authorsDd?.querySelector(".panel-list")).not.toBeNull();

    store.set({ selection: { kind: "node", key: "R1" } });
    expect(sectionTitles()).toEqual(["Общее", "Служебное"]);
  });

  it("гранты публикации — нумерованный список: первые три, остальные по кнопке", async () => {
    const data = await loadSampleGraphData();
    const funding = ["11-11-11111", "22-22-22222", "33-33-33333", "44-44-44444"].map((key) => ({
      funder: "Russian Science Foundation",
      grant_id: key,
      grant_key: key,
    }));
    const pubDetails = indexDetailsByKey(
      (await loadSamplePubDetails()).map((d) => (d.key === "P1" ? { ...d, funding } : d)),
    );
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "node", key: "P1" } });
    mountPanel(store, data, pubDetails, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    const list = () =>
      [...panel.querySelectorAll("dt")]
        .find((el) => el.textContent === "Гранты")
        ?.nextElementSibling?.querySelector(".panel-list");
    const grants = () => list()?.querySelectorAll("li:not(.panel-list__toggle)").length;
    expect(grants()).toBe(3);

    list()?.querySelector<HTMLButtonElement>(".panel-list__more")?.click();
    expect(grants()).toBe(4);
    expect(list()?.querySelector(".panel-list__more")?.textContent).toBe("− свернуть");
  });

  it("грант: номер в карточке публикации ведёт на карточку гранта со списком статей и CSV", async () => {
    const data = await loadSampleGraphData();
    const details = await loadSamplePubDetails();
    const grant = { funder: "Russian Science Foundation", grant_id: "Grant 18-19-00627", grant_key: "18-19-00627" };
    const pubDetails = indexDetailsByKey(
      details.map((d) =>
        d.key === "P1" || d.key === "P3"
          ? {
              ...d,
              funding: [
                grant,
                { funder: "RFBR", grant_id: "18-19-", grant_key: null },
                { funder: "Priority 2030", grant_id: null, grant_key: null },
              ],
            }
          : d,
      ),
    );
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "node", key: "P1" } });
    mountPanel(store, data, pubDetails, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    const grantsDd = [...panel.querySelectorAll("dt")].find((el) => el.textContent === "Гранты")?.nextElementSibling;
    expect(grantsDd?.textContent).toContain("Russian Science Foundation");
    expect(grantsDd?.textContent).toContain("18-19-"); // cut-short number shown, not clickable
    expect(grantsDd?.querySelectorAll(".panel-entity-ref")).toHaveLength(1);
    expect(grantsDd?.querySelector(".panel-entity-ref")?.textContent).toBe("18-19-00627");
    expect(grantsDd?.textContent).toContain("Priority 2030"); // funder without a number: plain text
    expect(grantsDd?.querySelector(".panel-list")?.children).toHaveLength(3); // numbered list, no toggle for 3

    grantsDd?.querySelector<HTMLButtonElement>(".panel-entity-ref")?.click();
    expect(store.get().selection).toEqual({ kind: "grant", key: "18-19-00627" });
    expect(store.get().tab).toBe(3);

    expect(panel.querySelector("h3")?.textContent).toBe("Russian Science Foundation Grant 18-19-00627");
    expect(panel.textContent).toContain("Russian Science Foundation");
    const csvLink = panel.querySelector<HTMLAnchorElement>("a[download]");
    expect(csvLink?.download).toBe("grant_18-19-00627.csv");
    const csv = decodeURIComponent(csvLink?.href.split(",").slice(1).join(",") ?? "");
    const lines = csv.replace("\uFEFF", "").split("\r\n");
    expect(lines[0]).toBe("OpenAlex ID,Title,Year,DOI,Journal,Type,Authors,Funder,Grant");
    expect(lines.slice(1).map((line) => line.split(",")[0]).sort()).toEqual(["P1", "P3"]);
  });

  it("грант из URL до прихода pubs-detail.json — индикатор загрузки, а не пустая панель", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "grant", key: "18-19-00627" } });
    mountPanel(store, data, NO_PUB_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);
    expect(panel.hidden).toBe(false);
    expect(panel.querySelector(".loading-indicator")).not.toBeNull();
  });

  it("карточка репозитория показывает описание (RepoDetail.description)", async () => {
    const data = await loadSampleGraphData();
    const repoDetails = indexDetailsByKey(await loadSampleRepoDetails());
    const repoDetail = repoDetails.get("R1");
    if (!repoDetail) throw new Error("repos-detail.sample.json должен содержать репозиторий R1");
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: "R1" },
    });

    mountPanel(store, data, NO_PUB_DETAILS, NO_AUTHOR_DETAILS, repoDetails);

    expect(panel.textContent).toContain(repoDetail.description);
  });

  it("карточка репозитория показывает тип владельца, лицензию и наличие README", async () => {
    const data = await loadSampleGraphData();
    // R1: has_readme=true, license="MIT", owner_type="organization".
    const repoDetails = indexDetailsByKey(await loadSampleRepoDetails());
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: "R1" },
    });

    mountPanel(store, data, NO_PUB_DETAILS, NO_AUTHOR_DETAILS, repoDetails);

    expect(panel.textContent).toContain("organization");
    expect(panel.textContent).toContain("MIT");
    expect(panel.textContent).toContain("✓");
  });

  it("не показывает строку лицензии у репозитория без неё (R2 во фикстуре)", async () => {
    const data = await loadSampleGraphData();
    const repoDetails = indexDetailsByKey(await loadSampleRepoDetails());
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: "R2" },
    });

    mountPanel(store, data, NO_PUB_DETAILS, NO_AUTHOR_DETAILS, repoDetails);

    expect(panel.textContent).not.toContain("Лицензия");
  });

  it("не показывает строки участников/публикаций у репозитория без единой связи", async () => {
    const data = await loadSampleGraphData();
    // R4 is in no repo_pub_edges.
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: "R4" },
    });

    mountPanel(store, data, NO_PUB_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    expect(panel.textContent).not.toContain("Публикации");
  });

  it("карточка публикации показывает список её авторов (all_edges)", async () => {
    const data = await loadSampleGraphData();
    // P1: authors A1 and A2 (all_edges).
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: "P1" },
    });

    mountPanel(store, data, NO_PUB_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    expect(panel.textContent).toContain("Иванов И.И.");
    expect(panel.textContent).toContain("Петрова А.С.");
  });

  it("показывает настоящее название публикации из pubDetails, а не её ключ", async () => {
    const data = await loadSampleGraphData();
    const pubDetails = indexDetailsByKey(await loadSamplePubDetails());
    const pub = data.pubs[0];
    if (!pub) throw new Error("фикстура должна содержать хотя бы одну публикацию");
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: pub.key },
    });

    mountPanel(store, data, pubDetails, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    const title = panel.querySelector("h3")?.textContent;
    expect(title).toBe(pubDetails.get(pub.key)?.label);
    expect(title).not.toBe(pub.key);
  });

  it("карточка публикации показывает тип, направления, аннотацию и ссылку на OpenAlex", async () => {
    const data = await loadSampleGraphData();
    const pubDetails = indexDetailsByKey(await loadSamplePubDetails());
    // P1: type="article", fields=["Computer Science"], non-empty abstract.
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: "P1" },
    });

    mountPanel(store, data, pubDetails, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    expect(panel.textContent).toContain("article");
    expect(panel.textContent).toContain("Computer Science");
    expect(panel.textContent).toContain("Краткое описание метода раскладки графов.");

    const openalexLink = panel.querySelector(
      "a[href='https://openalex.org/W1000000001']",
    ) as HTMLAnchorElement | null;
    expect(openalexLink?.textContent).toBe("OpenAlex");
  });

  it("показывает DOI и ссылку на код как кликабельные <a>, когда есть pubDetails и нет связанного репозитория", async () => {
    const data = await loadSampleGraphData();
    const pubDetails = indexDetailsByKey(await loadSamplePubDetails());
    // P4: has_code, one code_url, no repo_pub_edges, so the code shows as a link.
    const detail = pubDetails.get("P4");
    if (!detail?.has_code || detail.code_url.length === 0) {
      throw new Error(
        "фикстура pubs-detail.sample.json должна содержать P4 с has_code и хотя бы одним code_url",
      );
    }
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: "P4" },
    });

    mountPanel(store, data, pubDetails, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    const doiLink = panel.querySelector("a[href^='https://doi.org/']") as HTMLAnchorElement | null;
    expect(doiLink?.textContent).toBe(detail.doi);
    expect(doiLink?.target).toBe("_blank");
    expect(doiLink?.rel).toContain("noopener");

    const codeLink = panel.querySelector(
      `a[href="${detail.code_url[0]}"]`,
    ) as HTMLAnchorElement | null;
    expect(codeLink?.textContent).toBe(detail.code_url[0]?.replace("https://github.com/", ""));
  });

  it("показывает ссылку на связанный репозиторий ВМЕСТО code_url, когда публикация связана с репозиторием (repo_pub_edges)", async () => {
    const data = await loadSampleGraphData();
    const pubDetails = indexDetailsByKey(await loadSamplePubDetails());
    // P1 is linked to R1 and also has a code_url: the repo wins.
    const detail = pubDetails.get("P1");
    if (!detail?.has_code || detail.code_url.length === 0) {
      throw new Error("фикстура pubs-detail.sample.json должна содержать P1 с has_code и code_url");
    }
    const repo = data.repos.find((r) => r.key === "R1");
    if (!repo) throw new Error("фикстура должна содержать репозиторий R1");
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: "P1" },
    });

    mountPanel(store, data, pubDetails, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    expect(panel.textContent).toContain(repo.label);
    expect(panel.querySelector(`a[href="${detail.code_url[0]}"]`)).toBeNull();
  });

  it("заменяет code_url с небезопасной схемой (javascript:) на about:blank вместо того, чтобы класть её в href", async () => {
    const data = await loadSampleGraphData();
    // P2, not P1: P1's repo link would hide code_url entirely.
    const pub = data.pubs.find((p) => p.key === "P2");
    if (!pub) throw new Error("фикстура должна содержать публикацию P2");
    const malicious: PubDetail = {
      key: pub.key,
      label: "Тестовая публикация",
      journal: "",
      doi: "",
      has_code: true,
      code_url: ["javascript:alert(1)"],
      type: "",
      fields: [],
      funding: [],
      versions: [],
      openalex_url: "",
      abstract: "",
    };
    const pubDetails = new Map([[pub.key, malicious]]);
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: pub.key },
    });

    mountPanel(store, data, pubDetails, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    const codeLink = panel.querySelector(`dd a`) as HTMLAnchorElement | null;
    expect(codeLink?.getAttribute("href")).toBe("about:blank");
  });

  it("не показывает строку кода, когда has_code === false, но DOI всё равно показывает", async () => {
    const data = await loadSampleGraphData();
    const pubDetails = indexDetailsByKey(await loadSamplePubDetails());
    // P2: has_code false, empty code_url, has a DOI.
    const detail = pubDetails.get("P2");
    if (!detail || detail.has_code)
      throw new Error("фикстура должна содержать P2 с has_code: false");
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: "P2" },
    });

    mountPanel(store, data, pubDetails, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

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

    mountPanel(store, data, NO_PUB_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    expect(panel.hidden).toBe(false);
    expect(panel.textContent).toContain(String(edge.w));
  });

  it("клик по 'От'/'К' в карточке ребра переходит к этому узлу", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "edge", s: "A1", t: "A2", w: 2 },
    });

    mountPanel(store, data, NO_PUB_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    const toButton = [...panel.querySelectorAll("button.panel-entity-ref")].find(
      (button) => button.textContent === "Петрова А.С.",
    ) as HTMLButtonElement | undefined;
    if (!toButton)
      throw new Error("кнопка-ссылка на 'К' (А2, Петрова А.С.) должна быть в карточке ребра");

    toButton.click();

    expect(store.get().selection).toEqual({ kind: "node", key: "A2" });
  });

  it("карточка ребра автор-автор показывает список общих публикаций", async () => {
    const data = await loadSampleGraphData();
    // A1-A2: w=2 and exactly two shared pubs (P1, P5) in all_edges.
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "edge", s: "A1", t: "A2", w: 2 },
    });

    mountPanel(store, data, NO_PUB_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    expect(panel.textContent).toContain("Общие публикации");
    const dt = [...panel.querySelectorAll("dt")].find((el) => el.textContent === "Общие публикации");
    expect(dt?.classList.contains("panel-row--block")).toBe(true); // one per line, like the pubs list
    const items = [...(dt?.nextElementSibling?.querySelectorAll("button.panel-entity-ref") ?? [])];
    expect(items.map((el) => el.textContent).sort()).toEqual(["P1", "P5"]);
  });

  it("карточка ребра публикация-публикация показывает список общих авторов", async () => {
    const data = await loadSampleGraphData();
    // P1-P2: w=1, shared author A1 (Иванов И.И.).
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "edge", s: "P1", t: "P2", w: 1 },
    });

    mountPanel(store, data, NO_PUB_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    expect(panel.textContent).toContain("Общие авторы");
    expect(panel.textContent).toContain("Иванов И.И.");
  });

  it("не показывает строку общих публикаций, когда общих публикаций реально нет", async () => {
    const data = await loadSampleGraphData();
    // A3-A4: has a weight, but no shared pubs in all_edges, so no row.
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "edge", s: "A3", t: "A4", w: 1 },
    });

    mountPanel(store, data, NO_PUB_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    expect(panel.textContent).not.toContain("Общие публикации");
  });

  it("показывает карточку департамента со сводными числами", async () => {
    const data = await loadSampleGraphData();
    const dept = data.departments[0];
    if (!dept) throw new Error("фикстура должна содержать хотя бы один департамент");
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "dept", id: dept.id },
    });

    mountPanel(store, data, NO_PUB_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    expect(panel.hidden).toBe(false);
    expect(panel.querySelector("h3")?.textContent).toBe(dept.name);
    expect(panel.textContent).toContain(String(dept.n_authors));
    expect(panel.textContent).toContain(String(dept.n_repos));
    expect(panel.querySelector(".panel-kind")?.textContent).toBe("Департамент");
    expect(panel.querySelector(".panel-back")?.textContent).toBe("← Обзор");
  });

  it("карточка департамента показывает связанные департаменты по убыванию веса (dept_edges)", async () => {
    const data = await loadSampleGraphData();
    // Department 0 links to 1 (w=2) and 2 (w=1); 1 comes first.
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "dept", id: 0 } });

    mountPanel(store, data, NO_PUB_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    const dept1 = data.departments.find((d) => d.id === 1);
    const dept2 = data.departments.find((d) => d.id === 2);
    if (!dept1 || !dept2) throw new Error("фикстура должна содержать департаменты 1 и 2");

    const text = panel.textContent ?? "";
    expect(text).toContain("Связанные департаменты");
    const dt = [...panel.querySelectorAll("dt")].find(
      (el) => el.textContent === "Связанные департаменты",
    );
    expect(dt?.classList.contains("panel-row--block")).toBe(true); // a list with "+ N more", not comma-separated
    expect(text.indexOf(dept1.name)).toBeGreaterThan(-1);
    expect(text.indexOf(dept1.name)).toBeLessThan(text.indexOf(dept2.name));
  });

  it("клик по связанному департаменту делает его новым selection", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>({ ...initialState(), selection: { kind: "dept", id: 0 } });
    const dept1 = data.departments.find((d) => d.id === 1);
    if (!dept1) throw new Error("фикстура должна содержать департамент 1");

    mountPanel(store, data, NO_PUB_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);

    const dept1Button = [...panel.querySelectorAll("button.panel-entity-ref")].find(
      (button) => button.textContent === dept1.name,
    ) as HTMLButtonElement | undefined;
    if (!dept1Button) throw new Error("кнопка-ссылка на департамент 1 должна быть в карточке");

    dept1Button.click();

    expect(store.get().selection).toEqual({ kind: "dept", id: 1 });
  });

  it("возвращается к карточке «Обзор», когда selection сбрасывают в null", async () => {
    const data = await loadSampleGraphData();
    const author = data.authors[0];
    if (!author) throw new Error("фикстура должна содержать хотя бы одного автора");
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: author.key },
    });

    mountPanel(store, data, NO_PUB_DETAILS, NO_AUTHOR_DETAILS, NO_REPO_DETAILS);
    expect(panel.querySelector("h3")?.textContent).toBe(author.label);

    store.set({ selection: null });
    expect(panel.hidden).toBe(false);
    expect(panel.querySelector("h3")?.textContent).toBe("Обзор");
  });

  it("показывает индикатор загрузки, пока authorDetails ещё пуст (файл не домержился)", async () => {
    const data = await loadSampleGraphData();
    const author = data.authors[0];
    if (!author) throw new Error("фикстура должна содержать хотя бы одного автора");
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: author.key },
    });

    // Empty map: what app/main.ts passes before authors-detail.json arrives.
    mountPanel(store, data, NO_PUB_DETAILS, new Map(), NO_REPO_DETAILS);

    expect(panel.querySelector(".loading-indicator")).not.toBeNull();
    expect(panel.textContent).toContain("Подробнее");
  });

  it("после того как authorDetails домержился и пришёл store.notify(), индикатор сменяется реальными полями", async () => {
    const data = await loadSampleGraphData();
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: "A1" },
    });
    const authorDetails = new Map<string, AuthorDetail>(); // empty at mount time

    mountPanel(store, data, NO_PUB_DETAILS, authorDetails, NO_REPO_DETAILS);
    expect(panel.querySelector(".loading-indicator")).not.toBeNull();

    // Same as app/main.ts: merge into the same map, then notify().
    mergeDetailsInto(authorDetails, await loadSampleAuthorDetails());
    store.notify();

    expect(panel.querySelector(".loading-indicator")).toBeNull();
    expect(panel.textContent).toContain("к.т.н."); // A1.degree from authors-detail.sample.json
  });

  it("показывает индикатор загрузки для репозитория, пока repoDetails ещё пуст", async () => {
    const data = await loadSampleGraphData();
    const repo = data.repos[0];
    if (!repo) throw new Error("фикстура должна содержать хотя бы один репозиторий");
    const store = new Store<AppState>({
      ...initialState(),
      selection: { kind: "node", key: repo.key },
    });

    mountPanel(store, data, NO_PUB_DETAILS, NO_AUTHOR_DETAILS, new Map());

    expect(panel.querySelector(".loading-indicator")).not.toBeNull();
  });
});
