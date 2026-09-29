import type {
  Affiliation,
  AuthorDetail,
  AuthorNode,
  GraphData,
  PubDetail,
  PubNode,
  RepoDetail,
  RepoNode,
} from "../contracts/graph";
import { DATA_CONFIG, PANEL_CONFIG } from "../core/config";
import {
  buildAuthorPubIndex,
  buildAuthorRepoIndex,
  buildCoauthIndex,
  buildDeptEdgeIndex,
  buildRepoAuthorIndex,
  buildRepoPubIndex,
  githubProfileUrl,
  githubShortPath,
  grantIndex,
  groupsById,
  indexByKey,
  nodeLabel,
  toCsv,
  type GrantInfo,
} from "../core/data";
import { createLoadingIndicator, requireElement } from "../core/dom";
import { kindLabel, localize, t } from "../core/i18n";
import { TAB_FOR_KIND, type AppState, type Selection, type Store } from "../core/state";

/** The detail file has not arrived yet; differs from an empty value. */
const LOADING: unique symbol = Symbol("panel-row-loading");

/**
 * Plain text, external links (open in a new tab), refs to other graph
 * entities (a click selects them), or {@link LOADING}.
 */
type PanelRowValue =
  | string
  | PanelLink[]
  | (PanelEntityRef | PanelText)[]
  | PanelList
  | PanelLongText
  | typeof LOADING;
interface PanelLink {
  kind: "link";
  href: string;
  text: string;
  /** Non-clickable suffix, e.g. affiliation years. */
  meta?: string;
  /** File name: the link downloads instead of opening (grant CSV). */
  download?: string;
}
/** Full-width list with the first {@link PANEL_CONFIG.listLimit} items and a "N more" toggle. */
interface PanelList {
  kind: "list";
  items: (string | PanelText | PanelLink | PanelEntityRef)[];
}
/** First {@link PANEL_CONFIG.abstractWords} words, the rest on click. */
interface PanelLongText {
  kind: "longText";
  text: string;
}
/** Non-clickable {@link PanelList} item styled like {@link PanelLink}. */
interface PanelText {
  kind: "text";
  text: string;
  meta?: string;
}
/** Link to another node, department or grant; a click selects it. */
interface PanelEntityRef {
  kind: "ref";
  selection: Extract<Selection, { kind: "node" } | { kind: "dept" } | { kind: "grant" }>;
  label: string;
  /** Non-clickable suffix, e.g. a repo role. */
  meta?: string;
}
type PanelRow = [label: string, value: PanelRowValue];
/** `null` title means no heading. */
interface PanelSection {
  title: string | null;
  rows: PanelRow[];
}

function refKey(ref: PanelEntityRef): string {
  return ref.selection.kind === "node" ? ref.selection.key : "";
}

function untitled(rows: PanelRow[]): PanelSection[] {
  return [{ title: null, rows }];
}

/** Accepts a bare DOI or a full https://doi.org/ URL. */
function doiLink(doi: string): PanelLink {
  return {
    kind: "link",
    href: `https://doi.org/${doi.replace(/^https?:\/\/doi\.org\//, "")}`,
    text: doi,
  };
}

function githubLink(username: string): PanelLink {
  return { kind: "link", href: githubProfileUrl(username), text: username };
}

function orcidLink(id: string): PanelLink {
  return { kind: "link", href: `https://orcid.org/${id}`, text: id };
}

/**
 * For URLs from harvested data: anything but http/https (e.g. `javascript:`)
 * becomes `about:blank`, with a console warning.
 */
function safeHref(url: string, context: string): string {
  try {
    const parsed = new URL(url);
    if (parsed.protocol === "http:" || parsed.protocol === "https:") return parsed.toString();
    console.warn(`${context}: недопустимая схема, ссылка заменена на "about:blank": ${url}`);
  } catch {
    console.warn(
      `${context}: значение не распознано как URL, ссылка заменена на "about:blank": ${url}`,
    );
  }
  return "about:blank";
}

function codeLink(url: string): PanelLink {
  return { kind: "link", href: safeHref(url, "codeLink"), text: githubShortPath(url) };
}

/** The URL is long and full of query params, so the text is fixed. */
function googleScholarLink(url: string): PanelLink {
  return { kind: "link", href: safeHref(url, "googleScholarLink"), text: "Google Scholar" };
}

function openalexUrlLink(url: string): PanelLink {
  return { kind: "link", href: safeHref(url, "openalexUrlLink"), text: "OpenAlex" };
}

function openalexIdLink(id: string): PanelLink {
  return { kind: "link", href: `https://openalex.org/${id}`, text: id };
}

function emailLink(email: string): PanelLink {
  return { kind: "link", href: `mailto:${email}`, text: email };
}

const AFFILIATION_SOURCE_LABELS: Record<string, string> = { openalex: "OpenAlex", orcid: "ORCID" };

/**
 * Merges OpenAlex and ORCID entries with the same name into one item: name
 * (linked to ror.org when known), year range and sources. Newest first.
 */
function affiliationItems(affiliations: Affiliation[]): (PanelLink | PanelText)[] {
  const byName = new Map<string, { ror: string; years: number[]; sources: Set<string> }>();
  for (const aff of affiliations) {
    const entry = byName.get(aff.name) ?? { ror: "", years: [], sources: new Set<string>() };
    entry.ror ||= aff.ror;
    entry.years.push(...aff.years);
    entry.sources.add(AFFILIATION_SOURCE_LABELS[aff.source] ?? aff.source);
    byName.set(aff.name, entry);
  }

  return [...byName.entries()]
    .map(([name, { ror, years, sources }]) => ({
      name,
      ror,
      first: years.length > 0 ? Math.min(...years) : null,
      last: years.length > 0 ? Math.max(...years) : null,
      sources: [...sources],
    }))
    .sort((a, b) => (b.last ?? -Infinity) - (a.last ?? -Infinity))
    .map(({ name, ror, first, last, sources }): PanelLink | PanelText => {
      const yearsText = first === null ? "" : first === last ? String(first) : `${first}–${last}`;
      const meta = [yearsText, sources.join(", ")].filter(Boolean).join(" · ");
      return ror
        ? { kind: "link", href: `https://ror.org/${encodeURIComponent(ror)}`, text: name, meta }
        : { kind: "text", text: name, meta };
    });
}

/** Unparseable strings are returned as is. */
function formatTimestamp(value: string, lang: AppState["lang"]): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(lang === "ru" ? "ru-RU" : "en-GB", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** @returns Unmount function. */
export function mountPanel(
  store: Store<AppState>,
  data: GraphData,
  pubDetails: Map<string, PubDetail>,
  authorDetails: Map<string, AuthorDetail>,
  repoDetails: Map<string, RepoDetail>,
): () => void {
  const container = requireElement("panel");

  // ponytail: static PDFs + file list next to the site data, move into repos-detail.json if this stays
  const reportFiles = new Map<string, string>();
  fetch(DATA_CONFIG.reportsIndexUrl)
    .then((response) => (response.ok ? (response.json() as Promise<string[]>) : []))
    .then((files) => {
      for (const file of files) reportFiles.set(file.toLowerCase(), file);
      store.notify();
    })
    .catch((error: unknown) => console.warn("reports/index.json:", error));

  // ponytail: static file from paper_analysis run.json, move onto the IMPLEMENTS edge if this stays
  const implementationRates = new Map<
    string,
    { implemented: number; total: number; pct: number }
  >();
  fetch(DATA_CONFIG.implementationRatesUrl)
    .then((response) =>
      response.ok
        ? (response.json() as Promise<
            { pub: string; repo: string; implemented: number; total: number; pct: number }[]
          >)
        : [],
    )
    .then((rows) => {
      for (const { pub, repo, ...rate } of rows)
        implementationRates.set(`${repo}\u0000${pub}`, rate);
      store.notify();
    })
    .catch((error: unknown) => console.warn("implementation-rates.json:", error));

  /** Repo/pub refs with the implementation rate in `meta`: "implemented 32/57 · 56%". */
  function withImplementationRate(
    refs: PanelEntityRef[],
    pairKeyOf: (ref: PanelEntityRef) => string,
    lang: AppState["lang"],
  ): PanelEntityRef[] {
    return refs.map((ref) => {
      const rate = implementationRates.get(pairKeyOf(ref));
      return rate
        ? {
            ...ref,
            meta: `${t("field.implemented", lang)} ${rate.implemented}/${rate.total} · ${rate.pct}%`,
          }
        : ref;
    });
  }

  /** The number is clickable only when it was normalized (grant_key). */
  function fundingItem(entry: PubDetail["funding"][number]): PanelEntityRef | PanelText {
    if (entry.grant_key)
      return {
        kind: "ref",
        selection: { kind: "grant", key: entry.grant_key },
        label: entry.grant_key,
        meta: entry.funder,
      };
    const number = (entry.grant_id ?? "").trim();
    return number
      ? { kind: "text", text: number, meta: entry.funder }
      : { kind: "text", text: entry.funder };
  }

  /** One CSV row per publication, newest first. */
  function grantCsvLink(grant: GrantInfo, pubKeys: string[], lang: AppState["lang"]): PanelLink {
    const rows = [
      ["OpenAlex ID", "Title", "Year", "DOI", "Journal", "Type", "Authors", "Funder", "Grant"],
    ];
    for (const key of pubKeys) {
      const node = index.get(key);
      const detail = pubDetails.get(key);
      const authors = (pubAuthors.get(key) ?? []).map((author) => {
        const authorNode = index.get(author);
        return authorNode ? nodeLabel(authorNode, lang) : author;
      });
      const entry = detail?.funding.find((f) => f.grant_key === grant.key);
      rows.push([
        key,
        detail?.label ?? "",
        node?.kind === "pub" && node.year !== null ? String(node.year) : "",
        detail?.doi ?? "",
        detail?.journal ?? "",
        detail?.type ?? "",
        authors.join("; "),
        entry?.funder ?? grant.funder,
        entry?.grant_id?.trim() || grant.name,
      ]);
    }
    return {
      kind: "link",
      href: `data:text/csv;charset=utf-8,${encodeURIComponent(toCsv(rows))}`,
      text: t("grant.downloadCsv", lang),
      download: `grant_${grant.key.replace(/[^0-9A-Za-z.-]/g, "_")}.csv`,
    };
  }

  function reportLinksOf(repoUrl: string): PanelLink[] {
    const name = repoUrl.replace(/\/+$/, "").split("/").pop()?.toLowerCase() ?? "";
    const file = reportFiles.get(`${name}_report.pdf`);
    return file
      ? [
          {
            kind: "link",
            href: `/reports/${encodeURIComponent(file)}`,
            text: "PDF",
          },
        ]
      : [];
  }

  const index = indexByKey(data);
  const deptById = new Map(data.departments.map((dept) => [dept.id, dept]));
  const groups = groupsById(data);
  const repoGroupById = new Map((data.repo_groups ?? []).map((group) => [group.id, group]));
  const repoEdgeVia = new Map(
    data.repo_edges.flatMap((edge) => [
      [`${edge.s}\u0000${edge.t}`, edge.via ?? []],
      [`${edge.t}\u0000${edge.s}`, edge.via ?? []],
    ]),
  );
  const { authorPubs, pubAuthors } = buildAuthorPubIndex(data);
  const coauthIndex = buildCoauthIndex(data);
  const authorRepoIndex = buildAuthorRepoIndex(data);
  const deptEdgeIndex = buildDeptEdgeIndex(data);
  const repoAuthorIndex = buildRepoAuthorIndex(data);
  const { repoPubs: repoPubIndex, pubRepos: pubRepoIndex } = buildRepoPubIndex(data);

  function deptRefsOf(ids: number[], lang: AppState["lang"]): PanelEntityRef[] {
    return ids.map((id) => {
      const dept = groups.get(id);
      const label = dept ? localize(dept.name, dept.name_en, lang) : String(id);
      return { kind: "ref", selection: { kind: "dept", id }, label };
    });
  }

  /** An unknown key keeps its raw text as the label. */
  function entityRefsOf(keys: string[], lang: AppState["lang"]): PanelEntityRef[] {
    return keys.map((key) => {
      const node = index.get(key);
      const label = node ? nodeLabel(node, lang, pubDetails) : key;
      return { kind: "ref", selection: { kind: "node", key }, label };
    });
  }

  function recentPubKeysFrom(pubKeys: string[]): string[] {
    return pubKeysByYear(pubKeys).slice(0, PANEL_CONFIG.listLimit);
  }

  function pubKeysByYear(pubKeys: string[]): string[] {
    return pubKeys
      .map((key) => index.get(key))
      .filter((node): node is PubNode => node?.kind === "pub")
      .sort((a, b) => (b.year ?? -Infinity) - (a.year ?? -Infinity))
      .map((node) => node.key);
  }

  function topCoauthorKeys(authorKey: string): string[] {
    return [...(coauthIndex.get(authorKey) ?? new Map<string, number>()).entries()]
      .sort(([, weightA], [, weightB]) => weightB - weightA)
      .map(([key]) => key);
  }

  function authorRepoKeysOf(authorKey: string): string[] {
    return (authorRepoIndex.get(authorKey) ?? [])
      .map((key) => index.get(key))
      .filter((node): node is RepoNode => node?.kind === "repo")
      .sort((a, b) => b.stars - a.stars)
      .map((node) => node.key);
  }

  /** Clickable name with the role as a suffix, e.g. "Ivanov I.I. (maintainer)". */
  function repoContributorRefsOf(repoKey: string, lang: AppState["lang"]): PanelEntityRef[] {
    return (repoAuthorIndex.get(repoKey) ?? []).slice(0, PANEL_CONFIG.listLimit).map((edge) => {
      const author = index.get(edge.t);
      const label = author ? nodeLabel(author, lang, pubDetails) : edge.t;
      return {
        kind: "ref",
        selection: { kind: "node", key: edge.t },
        label,
        meta: `(${edge.role})`,
      };
    });
  }

  function repoPubKeysOf(repoKey: string): string[] {
    return recentPubKeysFrom(repoPubIndex.get(repoKey) ?? []);
  }

  function hide(): void {
    container.hidden = true;
    container.replaceChildren();
  }

  /**
   * @param kind - Badge next to the title, so the user sees what kind of
   *   entity they jumped to.
   * @param showBack - Every card but the overview gets a back button.
   */
  function show(
    title: string,
    kind: string,
    sections: PanelSection[],
    showBack: boolean,
    subtitle?: string | null,
    extra?: HTMLElement | null,
  ): void {
    container.hidden = false;
    container.replaceChildren(
      buildCard({
        title,
        kind,
        sections,
        lang: store.get().lang,
        backLabel: showBack ? `← ${t("overview.title", store.get().lang)}` : null,
        // A ref to another kind also switches the tab, otherwise the node is
        // missing from the current graph and the camera cannot fly to it.
        // Department anchors exist in every tab.
        onSelectRef: (selection) => {
          if (selection?.kind === "grant") {
            store.set({ tab: TAB_FOR_KIND.pub, selection });
            return;
          }
          if (selection?.kind === "node") {
            const node = index.get(selection.key);
            if (node) {
              store.set({ tab: TAB_FOR_KIND[node.kind], selection });
              return;
            }
          }
          store.set({ selection });
        },
        onBack: () => store.set({ selection: null }),
        subtitle,
        extra,
      }),
    );
  }

  /**
   * The denominator is the number of entities whose detail already arrived,
   * so the percentage does not creep up while the file loads.
   */
  function completionRow(count: number, total: number): PanelRowValue {
    return total === 0 ? LOADING : `${Math.round((count / total) * 100)}%`;
  }

  /** Overview of the active tab, shown when nothing is selected. */
  function renderOverview(state: AppState): void {
    const { tab, lang } = state;
    const tabKind =
      tab === 1 ? t("tab.authors", lang) : tab === 2 ? t("tab.repos", lang) : t("tab.pubs", lang);

    if (tab === 1) {
      // All authors, ITMO and external, regardless of the map filter.
      const authors: AuthorNode[] = data.authors;
      const external = authors.filter((a) => a.is_itmo === false).length;
      const avgPubs =
        authors.length > 0
          ? (authors.reduce((sum, a) => sum + a.pubs_count, 0) / authors.length).toFixed(1)
          : "0";

      let withDetail = 0;
      let withOrcid = 0;
      let withGithub = 0;
      let withEmail = 0;
      for (const author of authors) {
        const detail = authorDetails.get(author.key);
        if (!detail) continue;
        withDetail++;
        if (detail.orcid) withOrcid++;
        if (detail.github) withGithub++;
        if (detail.email) withEmail++;
      }

      return show(
        t("overview.title", lang),
        tabKind,
        untitled([
          [t("field.authorsCount", lang), String(authors.length)],
          [t("overview.itmoAuthors", lang), String(authors.length - external)],
          [t("overview.externalAuthors", lang), String(external)],
          [t("field.deptsCount", lang), String(data.departments.length)],
          [t("overview.avgPubsPerAuthor", lang), avgPubs],
          [t("field.orcid", lang), completionRow(withOrcid, withDetail)],
          [t("field.github", lang), completionRow(withGithub, withDetail)],
          [t("field.email", lang), completionRow(withEmail, withDetail)],
        ]),
        false,
        null,
        authorsByDeptChart(authors, lang),
      );
    }

    if (tab === 2) {
      const repos: RepoNode[] = data.repos;

      let withReadme = 0;
      let withLicense = 0;
      for (const detail of repoDetails.values()) {
        if (detail.has_readme) withReadme++;
        if (detail.license) withLicense++;
      }

      return show(
        t("overview.title", lang),
        tabKind,
        untitled([
          [t("field.reposCount", lang), String(repos.length)],
          [t("field.hasReadme", lang), completionRow(withReadme, repoDetails.size)],
          [t("field.license", lang), completionRow(withLicense, repoDetails.size)],
        ]),
        false,
        null,
        reposByStarsChart(repos, lang),
      );
    }

    const pubs: PubNode[] = data.pubs;
    const withKnownYear = pubs.filter((p) => p.year !== null).length;

    let withDoi = 0;
    let withAbstract = 0;
    for (const detail of pubDetails.values()) {
      if (detail.doi) withDoi++;
      if (detail.abstract) withAbstract++;
    }

    return show(
      t("overview.title", lang),
      tabKind,
      untitled([
        [t("field.pubsCount", lang), String(pubs.length)],
        [t("overview.knownYear", lang), completionRow(withKnownYear, pubs.length)],
        [t("field.doi", lang), completionRow(withDoi, pubDetails.size)],
        [t("field.abstract", lang), completionRow(withAbstract, pubDetails.size)],
      ]),
      false,
      null,
      pubsByYearChart(pubs, lang),
    );
  }

  function authorsByDeptChart(authors: AuthorNode[], lang: AppState["lang"]): HTMLElement | null {
    const deptById = new Map(data.departments.map((dept) => [dept.id, dept]));
    const countByDept = new Map<number, number>();
    for (const author of authors) {
      countByDept.set(author.dept, (countByDept.get(author.dept) ?? 0) + 1);
    }

    const bars = [...countByDept.entries()]
      .sort((a, b) => b[1] - a[1])
      .slice(0, PANEL_CONFIG.chartBars)
      .map(([deptId, count]) => {
        const dept = deptById.get(deptId);
        return {
          label: dept ? localize(dept.name, dept.name_en, lang) : t("field.unknownDept", lang),
          value: count,
        };
      });

    return buildBarChart(t("chart.authorsByDept", lang), bars);
  }

  /** Chronological and not truncated: it is a time series. Unknown years are left out. */
  function pubsByYearChart(pubs: PubNode[], lang: AppState["lang"]): HTMLElement | null {
    const countByYear = new Map<number, number>();
    for (const pub of pubs) {
      if (pub.year === null) continue;
      countByYear.set(pub.year, (countByYear.get(pub.year) ?? 0) + 1);
    }

    const bars = [...countByYear.entries()]
      .sort((a, b) => a[0] - b[0])
      .map(([year, count]) => ({ label: String(year), value: count }));

    return buildBarChart(t("chart.pubsByYear", lang), bars);
  }

  /** Roughly logarithmic: a few repos have thousands of stars, most have a handful. */
  const STAR_BUCKETS: { max: number; label: string }[] = [
    { max: 0, label: "0" },
    { max: 9, label: "1–9" },
    { max: 99, label: "10–99" },
    { max: 999, label: "100–999" },
    { max: Infinity, label: "1000+" },
  ];

  function reposByStarsChart(repos: RepoNode[], lang: AppState["lang"]): HTMLElement | null {
    const countByBucket = new Map<string, number>(STAR_BUCKETS.map((bucket) => [bucket.label, 0]));
    for (const repo of repos) {
      const bucket = STAR_BUCKETS.find((b) => repo.stars <= b.max);
      if (bucket) countByBucket.set(bucket.label, (countByBucket.get(bucket.label) ?? 0) + 1);
    }

    const bars = STAR_BUCKETS.map((bucket) => ({
      label: bucket.label,
      value: countByBucket.get(bucket.label) ?? 0,
    }));
    return buildBarChart(t("chart.reposByStars", lang), bars);
  }

  /** A selection missing from `data` hides the panel instead of showing an empty card. */
  function render(state: AppState): void {
    const { selection, lang } = state;
    if (selection === null) return renderOverview(state);

    if (selection.kind === "node") {
      const node = index.get(selection.key);
      if (!node) return hide();

      const dept = deptById.get(node.dept);
      let title = nodeLabel(node, lang, pubDetails);
      let subtitle: string | null = null;
      const keyRow: PanelRow = [t("field.key", lang), node.key];
      const kindRow: PanelRow = [t("field.kind", lang), kindLabel(node.kind, lang)];
      const deptRow: PanelRow = [
        t("field.dept", lang),
        dept ? localize(dept.name, dept.name_en, lang) : t("field.unknownDept", lang),
      ];
      const rows: PanelRow[] = [keyRow, kindRow, deptRow];
      if (node.kind === "author") {
        // Sections follow the field tags in pauk/cache/export.py: public and
        // graph links, private, service fields.
        const general: PanelRow[] = [
          deptRow,
          [t("field.pubsCount", lang), String(node.pubs_count)],
        ];
        const privateRows: PanelRow[] = [];
        const service: PanelRow[] = [keyRow, kindRow];

        // Every author has a record, even with empty fields, so a missing
        // record means the file has not arrived yet.
        const authorDetail = authorDetails.get(node.key);
        if (authorDetail) {
          const fullName = localize(authorDetail.name_ru, authorDetail.name_en, lang);
          if (fullName) {
            title = fullName;
            const otherName = localize(authorDetail.name_en, authorDetail.name_ru, lang);
            if (otherName && otherName !== fullName) subtitle = otherName;
          }
          if (authorDetail.openalex_id)
            general.push([t("field.openalexId", lang), [openalexIdLink(authorDetail.openalex_id)]]);

          if (authorDetail.degree) privateRows.push([t("field.degree", lang), authorDetail.degree]);
          if (authorDetail.github)
            privateRows.push([t("field.github", lang), [githubLink(authorDetail.github)]]);
          if (authorDetail.orcid)
            privateRows.push([t("field.orcid", lang), [orcidLink(authorDetail.orcid)]]);
          if (authorDetail.google_scholar) {
            privateRows.push([
              t("field.googleScholar", lang),
              [googleScholarLink(authorDetail.google_scholar)],
            ]);
          }
          if (authorDetail.email)
            privateRows.push([t("field.email", lang), [emailLink(authorDetail.email)]]);
          if (authorDetail.affiliations.length > 0) {
            privateRows.push([
              t("field.affiliations", lang),
              { kind: "list", items: affiliationItems(authorDetail.affiliations) },
            ]);
          }
          // Per source, see author_variants() in pauk/gui/graph_builder/nodes.py.
          if (authorDetail.name_variants.openalex.length > 0) {
            privateRows.push([
              t("field.nameVariantsOpenalex", lang),
              { kind: "list", items: authorDetail.name_variants.openalex },
            ]);
          }
          if (authorDetail.name_variants.orcid.length > 0) {
            privateRows.push([
              t("field.nameVariantsOrcid", lang),
              { kind: "list", items: authorDetail.name_variants.orcid },
            ]);
          }

          if (authorDetail.created_at)
            service.push([
              t("field.createdAt", lang),
              formatTimestamp(authorDetail.created_at, lang),
            ]);
          if (authorDetail.updated_at)
            service.push([
              t("field.updatedAt", lang),
              formatTimestamp(authorDetail.updated_at, lang),
            ]);
        } else {
          privateRows.push([t("field.loadingDetails", lang), LOADING]);
        }

        const pubKeys = pubKeysByYear(authorPubs.get(node.key) ?? []);
        if (pubKeys.length > 0) {
          const pubRefs = entityRefsOf(pubKeys, lang).map((ref, i) => {
            const role = authorDetail?.pub_roles?.[pubKeys[i] ?? ""];
            const meta = role
              ? [
                  role.position === null
                    ? ""
                    : t("field.authorPosition", lang).replace("{n}", String(role.position)),
                  role.corresponding ? t("field.corresponding", lang) : "",
                ]
                  .filter(Boolean)
                  .join(" · ")
              : "";
            return meta ? { ...ref, meta } : ref;
          });
          general.push([t("tab.pubs", lang), { kind: "list", items: pubRefs }]);
        }

        const coauthors = topCoauthorKeys(node.key);
        if (coauthors.length > 0) {
          general.push([
            t("field.topCoauthors", lang),
            { kind: "list", items: entityRefsOf(coauthors, lang) },
          ]);
        }

        const authorRepos = authorRepoKeysOf(node.key);
        if (authorRepos.length > 0) {
          general.push([
            t("tab.repos", lang),
            { kind: "list", items: entityRefsOf(authorRepos, lang) },
          ]);
        }

        return show(
          title,
          kindLabel(node.kind, lang),
          [
            { title: t("section.general", lang), rows: general },
            { title: t("section.private", lang), rows: privateRows },
            { title: t("section.service", lang), rows: service },
          ],
          true,
          subtitle,
        );
      }
      if (node.kind === "repo") {
        rows.push([t("field.stars", lang), String(node.stars)]);
        const group = node.group === undefined ? undefined : repoGroupById.get(node.group);
        if (group) rows.push([t(`group.kind.${group.kind}`, lang), deptRefsOf([group.id], lang)]);
        // Every repo has a record, see the author card above.
        if (repoDetails.has(node.key)) {
          const repoDetail = repoDetails.get(node.key);
          if (repoDetail?.url) rows.push([t("field.github", lang), [codeLink(repoDetail.url)]]);
          if (repoDetail?.description)
            rows.push([t("field.description", lang), repoDetail.description]);
          if (repoDetail?.owner_type)
            rows.push([t("field.ownerType", lang), repoDetail.owner_type]);
          if (repoDetail?.license) rows.push([t("field.license", lang), repoDetail.license]);
          if (repoDetail?.has_readme) rows.push([t("field.hasReadme", lang), "✓"]);
          const reportLinks = repoDetail ? reportLinksOf(repoDetail.url) : [];
          if (reportLinks.length > 0) rows.push([t("field.report", lang), reportLinks]);
        } else {
          rows.push([t("field.loadingDetails", lang), LOADING]);
        }

        const contributors = repoContributorRefsOf(node.key, lang);
        if (contributors.length > 0)
          rows.push([t("field.contributors", lang), { kind: "list", items: contributors }]);

        const repoPubs = repoPubKeysOf(node.key);
        if (repoPubs.length > 0)
          rows.push([
            t("tab.pubs", lang),
            {
              kind: "list",
              items: withImplementationRate(
                entityRefsOf(repoPubs, lang),
                (ref) => `${node.key}\u0000${refKey(ref)}`,
                lang,
              ),
            },
          ]);
      }
      if (node.kind === "pub") {
        rows.push([
          t("field.year", lang),
          node.year === null ? t("field.yearUnknown", lang) : String(node.year),
        ]);

        const detail = pubDetails.get(node.key);
        if (detail?.doi) rows.push([t("field.doi", lang), [doiLink(detail.doi)]]);
        if (detail?.type) rows.push([t("field.pubType", lang), detail.type]);
        if (detail && detail.fields.length > 0)
          rows.push([t("field.pubFields", lang), detail.fields.join(", ")]);
        if (detail && detail.funding.length > 0)
          rows.push([t("field.grants", lang), detail.funding.map(fundingItem)]);
        if (detail?.abstract)
          rows.push([t("field.abstract", lang), { kind: "longText", text: detail.abstract }]);
        if (detail?.openalex_url)
          rows.push([t("field.openalexUrl", lang), [openalexUrlLink(detail.openalex_url)]]);

        // A link to our own repo replaces code_url: our data is more reliable
        // than harvested links.
        const pubRepoKeys = (pubRepoIndex.get(node.key) ?? []).slice(0, PANEL_CONFIG.listLimit);
        if (pubRepoKeys.length > 0) {
          rows.push([
            t("tab.repos", lang),
            {
              kind: "list",
              items: withImplementationRate(
                entityRefsOf(pubRepoKeys, lang),
                (ref) => `${refKey(ref)}\u0000${node.key}`,
                lang,
              ),
            },
          ]);
        } else if (detail?.has_code && detail.code_url.length > 0) {
          rows.push([t("field.code", lang), detail.code_url.map(codeLink)]);
        }

        const pubAuthorKeys = (pubAuthors.get(node.key) ?? []).slice(0, PANEL_CONFIG.listLimit);
        if (pubAuthorKeys.length > 0)
          rows.push([t("tab.authors", lang), entityRefsOf(pubAuthorKeys, lang)]);
      }

      return show(title, kindLabel(node.kind, lang), untitled(rows), true, subtitle);
    }

    if (selection.kind === "edge") {
      const from = index.get(selection.s);
      const to = index.get(selection.t);
      if (!from || !to) return hide();

      const rows: PanelRow[] = [
        [t("field.edgeFrom", lang), entityRefsOf([from.key], lang)],
        [t("field.edgeTo", lang), entityRefsOf([to.key], lang)],
        [t("field.edgeWeight", lang), String(selection.w)],
      ];

      const via = repoEdgeVia.get(`${from.key}\u0000${to.key}`) ?? [];
      if (via.length > 0)
        rows.push([
          t("field.repoVia", lang),
          via.map((signal) => t(`via.${signal}`, lang)).join(", "),
        ]);
      if (from.kind === "author" && to.kind === "author") {
        const shared = (authorPubs.get(from.key) ?? [])
          .filter((pub) => (authorPubs.get(to.key) ?? []).includes(pub))
          .slice(0, PANEL_CONFIG.listLimit);
        if (shared.length > 0) rows.push([t("field.sharedPubs", lang), entityRefsOf(shared, lang)]);
      } else if (from.kind === "pub" && to.kind === "pub") {
        const shared = (pubAuthors.get(from.key) ?? [])
          .filter((author) => (pubAuthors.get(to.key) ?? []).includes(author))
          .slice(0, PANEL_CONFIG.listLimit);
        if (shared.length > 0)
          rows.push([t("field.sharedAuthors", lang), entityRefsOf(shared, lang)]);
      }

      return show(t("kind.edge", lang), t("kind.edge", lang), untitled(rows), true);
    }

    if (selection.kind === "grant") {
      const grant = grantIndex(pubDetails).get(selection.key);
      if (!grant) {
        // Grants come from pubs-detail.json; wait for it instead of hiding.
        if (pubDetails.size > 0) return hide();
        return show(
          selection.key,
          t("grant.kind", lang),
          untitled([[t("field.loadingDetails", lang), LOADING]]),
          true,
        );
      }
      const pubKeys = [...grant.pubs].sort((a, b) => {
        const yearOf = (key: string): number => {
          const node = index.get(key);
          return node?.kind === "pub" ? (node.year ?? 0) : 0;
        };
        return yearOf(b) - yearOf(a) || a.localeCompare(b);
      });
      return show(
        grant.name,
        t("grant.kind", lang),
        untitled([
          [t("grant.funder", lang), grant.funder],
          [t("field.pubsCount", lang), String(pubKeys.length)],
          [t("grant.export", lang), [grantCsvLink(grant, pubKeys, lang)]],
          [t("tab.pubs", lang), { kind: "list", items: entityRefsOf(pubKeys, lang) }],
        ]),
        true,
      );
    }

    const group = repoGroupById.get(selection.id);
    if (group) {
      const members = data.repos
        .filter((repo) => repo.group === group.id)
        .sort((a, b) => b.stars - a.stars)
        .map((repo) => repo.key);
      return show(
        localize(group.name, group.name_en, lang),
        t(`group.kind.${group.kind}`, lang),
        untitled([
          ...(group.kind === "org"
            ? [[t("field.github", lang), [githubLink(group.name)]] satisfies PanelRow]
            : []),
          [t("field.reposCount", lang), String(members.length)],
          [t("field.groupWhy", lang), t(`group.why.${group.kind}`, lang)],
          [t("tab.repos", lang), { kind: "list", items: entityRefsOf(members, lang) }],
        ]),
        true,
      );
    }
    const dept = deptById.get(selection.id);
    if (!dept) return hide();

    const rows: PanelRow[] = [
      [t("field.authorsCount", lang), String(dept.n_authors)],
      [t("field.pubsCount", lang), String(dept.n_pubs)],
      [t("field.reposCount", lang), String(dept.n_repos)],
      [t("field.total", lang), String(dept.n)],
    ];

    const relatedIds = [...(deptEdgeIndex.get(dept.id) ?? new Map<number, number>()).entries()]
      .sort(([, weightA], [, weightB]) => weightB - weightA)
      .map(([id]) => id);
    if (relatedIds.length > 0) {
      rows.push([
        t("field.relatedDepts", lang),
        { kind: "list", items: deptRefsOf(relatedIds, lang) },
      ]);
    }

    return show(
      localize(dept.name, dept.name_en, lang),
      kindLabel("dept", lang),
      untitled(rows),
      true,
    );
  }

  render(store.get());
  const unsubscribe = store.subscribe(render);
  return unsubscribe;
}

interface PanelCardOptions {
  title: string;
  kind: string;
  /** Empty sections are not drawn. */
  sections: PanelSection[];
  lang: AppState["lang"];
  /** `null` hides the back button. */
  backLabel: string | null;
  onSelectRef: (selection: Selection) => void;
  onBack: () => void;
  /** Name in the other language, small and grey under the title. */
  subtitle?: string | null;
  /** Appended after the `<dl>`, e.g. overview charts. */
  extra?: HTMLElement | null;
}

/** No `innerHTML`: graph data must never be parsed as markup. */
function buildCard(options: PanelCardOptions): HTMLElement {
  const { title, kind, sections, lang, backLabel, onSelectRef, onBack, subtitle, extra } = options;
  const card = document.createElement("div");
  card.className = "panel-card";

  if (backLabel !== null) {
    const back = document.createElement("button");
    back.type = "button";
    back.className = "panel-back";
    back.textContent = backLabel;
    back.addEventListener("click", onBack);
    card.appendChild(back);
  }

  // One block, so the subtitle sits right under the name.
  const head = document.createElement("div");
  head.className = "panel-card__head";
  const titles = document.createElement("div");
  titles.className = "panel-card__titles";
  const heading = document.createElement("h3");
  heading.textContent = title;
  titles.appendChild(heading);
  if (subtitle) {
    const sub = document.createElement("div");
    sub.className = "panel-card__subtitle";
    sub.textContent = subtitle;
    titles.appendChild(sub);
  }
  const kindBadge = document.createElement("span");
  kindBadge.className = "panel-kind";
  kindBadge.textContent = kind;
  head.append(titles, kindBadge);
  card.appendChild(head);

  function linkElement(link: PanelLink): HTMLAnchorElement {
    const a = document.createElement("a");
    a.href = link.href;
    if (link.download) a.download = link.download;
    else {
      a.target = "_blank";
      a.rel = "noopener noreferrer";
    }
    a.textContent = link.text;
    return a;
  }

  // <button>, not <a>: a click changes store.selection instead of navigating.
  function refElement(ref: PanelEntityRef): HTMLButtonElement {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "panel-entity-ref";
    button.textContent = ref.label;
    button.addEventListener("click", () => onSelectRef(ref.selection));
    return button;
  }

  function listItemElement(item: PanelList["items"][number]): HTMLLIElement {
    const li = document.createElement("li");
    if (typeof item === "string") {
      li.textContent = item;
      return li;
    }
    if (item.kind === "text") li.append(item.text);
    else li.appendChild(item.kind === "link" ? linkElement(item) : refElement(item));
    if (item.meta) {
      const meta = document.createElement("span");
      meta.className = "panel-list__meta";
      meta.textContent = item.meta;
      li.append(" ", meta);
    }
    return li;
  }

  function listElement(value: PanelList): HTMLUListElement {
    const ul = document.createElement("ul");
    ul.className = "panel-list";
    const limit = PANEL_CONFIG.listLimit;
    const hidden = value.items.length - limit;
    let expanded = false;

    function renderItems(): void {
      ul.replaceChildren(
        ...(expanded ? value.items : value.items.slice(0, limit)).map(listItemElement),
      );
      if (hidden <= 0) return;

      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "panel-list__more";
      toggle.textContent = expanded
        ? t("panel.showLess", lang)
        : t("panel.showMore", lang).replace("{n}", String(hidden));
      toggle.addEventListener("click", () => {
        expanded = !expanded;
        renderItems();
      });
      const toggleItem = document.createElement("li");
      toggleItem.appendChild(toggle);
      ul.appendChild(toggleItem);
    }

    renderItems();
    return ul;
  }

  function longTextElement(value: PanelLongText): HTMLDivElement {
    const el = document.createElement("div");
    const words = value.text.split(/\s+/).filter(Boolean);
    const limit = PANEL_CONFIG.abstractWords;
    let expanded = false;

    function render(): void {
      if (words.length <= limit) {
        el.textContent = value.text;
        return;
      }
      el.textContent = expanded ? value.text : `${words.slice(0, limit).join(" ")}…`;
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "panel-list__more";
      toggle.textContent = expanded ? t("panel.showLess", lang) : t("panel.readMore", lang);
      toggle.addEventListener("click", () => {
        expanded = !expanded;
        render();
      });
      el.append(" ", toggle);
    }

    render();
    return el;
  }

  for (const section of sections) {
    if (section.rows.length === 0) continue;

    const sectionEl = document.createElement("section");
    sectionEl.className = "panel-section";
    if (section.title) {
      const sectionTitle = document.createElement("h4");
      sectionTitle.className = "panel-section__title";
      sectionTitle.textContent = section.title;
      sectionEl.appendChild(sectionTitle);
    }

    const list = document.createElement("dl");
    for (const [label, value] of section.rows) {
      const dt = document.createElement("dt");
      dt.textContent = label;

      const dd = document.createElement("dd");
      if (typeof value === "string") {
        dd.textContent = value;
      } else if (value === LOADING) {
        dd.appendChild(createLoadingIndicator());
      } else if (!Array.isArray(value) && value.kind === "longText") {
        dt.classList.add("panel-row--block");
        dd.classList.add("panel-row--block");
        dd.appendChild(longTextElement(value));
      } else if (!Array.isArray(value)) {
        dt.classList.add("panel-row--block");
        dd.classList.add("panel-row--block");
        dd.appendChild(listElement(value));
      } else if (value[0]?.kind === "link") {
        (value as PanelLink[]).forEach((link, i) => {
          if (i > 0) dd.append(", ");
          dd.appendChild(linkElement(link));
        });
      } else {
        (value as (PanelEntityRef | PanelText)[]).forEach((item, i) => {
          if (i > 0) dd.append(", ");
          if (item.kind === "text") dd.append(item.text);
          else dd.appendChild(refElement(item));
          if (item.meta) {
            const meta = document.createElement("span");
            meta.className = "panel-list__meta";
            meta.textContent = item.meta;
            dd.append(" ", meta);
          }
        });
      }

      list.append(dt, dd);
    }
    sectionEl.appendChild(list);
    card.appendChild(sectionEl);
  }

  if (extra) card.appendChild(extra);

  return card;
}

/**
 * Plain DOM bar chart, widths in percent of the maximum.
 *
 * @param bars - Already sorted and truncated by the caller.
 * @returns `null` for no bars.
 */
function buildBarChart(
  title: string,
  bars: { label: string; value: number }[],
): HTMLElement | null {
  if (bars.length === 0) return null;
  // Avoids division by zero when every bar is 0.
  const max = Math.max(...bars.map((bar) => bar.value), 1);

  const container = document.createElement("div");
  container.className = "panel-chart";

  const heading = document.createElement("h4");
  heading.textContent = title;
  container.appendChild(heading);

  for (const bar of bars) {
    const row = document.createElement("div");
    row.className = "chart-bar-row";

    const label = document.createElement("span");
    label.className = "chart-bar-row__label";
    label.textContent = bar.label;

    const track = document.createElement("div");
    track.className = "chart-bar-row__track";
    const fill = document.createElement("div");
    fill.className = "chart-bar-row__fill";
    fill.style.width = `${(bar.value / max) * 100}%`;
    track.appendChild(fill);

    const value = document.createElement("span");
    value.className = "chart-bar-row__value";
    value.textContent = String(bar.value);

    row.append(label, track, value);
    container.appendChild(row);
  }

  return container;
}
