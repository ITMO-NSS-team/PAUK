import type {
  Affiliation,
  AuthorDetail,
  AuthorNode,
  GraphData,
  NodeKind,
  PubDetail,
  PubNode,
  RepoDetail,
  RepoNode,
  ReportsIndex,
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
import type { DetailResource } from "../core/details";
import { createLoadingIndicator, requireElement } from "../core/dom";
import { kindLabel, localize, t } from "../core/i18n";
import { TAB_FOR_KIND, type AppState, type Selection, type Store } from "../core/state";

/** The detail file has not arrived yet; differs from an empty value. */
const LOADING: unique symbol = Symbol("panel-row-loading");

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
  meta?: string;
  download?: string;
}
interface PanelList {
  nodeKind?: NodeKind;
  kind: "list";
  items: (string | PanelText | PanelLink | PanelEntityRef)[];
}
interface PanelLongText {
  kind: "longText";
  text: string;
}
interface PanelText {
  kind: "text";
  text: string;
  meta?: string;
}
interface PanelEntityRef {
  kind: "ref";
  selection: Extract<Selection, { kind: "node" } | { kind: "dept" } | { kind: "grant" }>;
  label: string;
  meta?: string;
}
type PanelRow = [label: string, value: PanelRowValue];
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

function safeHref(url: string, context: string): string {
  try {
    const parsed = new URL(url);
    if (parsed.protocol === "http:" || parsed.protocol === "https:") return parsed.toString();
    console.warn(`${context}: disallowed URL scheme, replaced with "about:blank": ${url}`);
  } catch {
    console.warn(`${context}: not a valid URL, replaced with "about:blank": ${url}`);
  }
  return "about:blank";
}

function codeLink(url: string): PanelLink {
  return { kind: "link", href: safeHref(url, "codeLink"), text: githubShortPath(url) };
}

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

export function mountPanel(
  store: Store<AppState>,
  data: GraphData,
  pubDetails: Map<string, PubDetail>,
  authorDetails: Map<string, AuthorDetail>,
  repoDetails: Map<string, RepoDetail>,
  resources?: Partial<Record<NodeKind, DetailResource>>,
): () => void {
  const container = requireElement("panel");

  // Written by `pauk reports build`, not by the site data build.
  const reportKeys = new Map<string, Set<string>>();
  fetch(DATA_CONFIG.reportsIndexUrl)
    .then((response) => (response.ok ? (response.json() as Promise<ReportsIndex>) : {}))
    .then((index: ReportsIndex) => {
      for (const [lang, keys] of Object.entries(index)) reportKeys.set(lang, new Set(keys));
      store.notify();
    })
    .catch((error: unknown) => console.warn("reports/index.json:", error));

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

  /** No fallback to another language: a report the reader can't read is no report. */
  function reportLinksOf(key: string, lang: AppState["lang"]): PanelLink[] {
    return reportKeys.get(lang)?.has(key)
      ? [
          {
            kind: "link",
            href: `${DATA_CONFIG.reportsUrl}/${lang}/${encodeURIComponent(key)}.pdf`,
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

  function entityRefsOf(keys: string[], lang: AppState["lang"]): PanelEntityRef[] {
    return keys.map((key) => {
      const node = index.get(key);
      const label = node ? nodeLabel(node, lang, pubDetails) : key;
      return { kind: "ref", selection: { kind: "node", key }, label };
    });
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

  function repoContributorRefsOf(repoKey: string, lang: AppState["lang"]): PanelEntityRef[] {
    return (repoAuthorIndex.get(repoKey) ?? []).map((edge) => {
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
    return pubKeysByYear(repoPubIndex.get(repoKey) ?? []);
  }

  function hide(): void {
    container.hidden = true;
    container.replaceChildren();
  }

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
        loading: () => {
          const state = store.get();
          const selection = state.selection;
          const kind =
            selection?.kind === "node"
              ? index.get(selection.key)?.kind
              : selection?.kind === "grant"
                ? "pub"
                : state.tab === 1
                  ? "author"
                  : state.tab === 2
                    ? "repo"
                    : "pub";
          const resource = kind ? resources?.[kind] : undefined;
          if (resource?.status !== "failed") return createLoadingIndicator();
          const message = document.createElement("span");
          message.append(`${t("details.failed", state.lang)} `);
          const retry = document.createElement("button");
          retry.type = "button";
          retry.textContent = t("details.retry", state.lang);
          retry.addEventListener("click", () => {
            void resource.load();
          });
          message.append(retry);
          return message;
        },
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

  function renderOverview(state: AppState): void {
    const { tab, lang } = state;
    const tabKind =
      tab === 1 ? t("tab.authors", lang) : tab === 2 ? t("tab.repos", lang) : t("tab.pubs", lang);

    if (tab === 1) {
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
      let hasContacts = false;
      for (const author of authors) {
        const detail = authorDetails.get(author.key);
        if (!detail) continue;
        withDetail++;
        if (detail.email !== undefined) hasContacts = true;
        if (detail.orcid) withOrcid++;
        if (detail.github) withGithub++;
        if (detail.email) withEmail++;
      }
      // The public build has no contact fields at all; 0% would read as "nobody has one".
      const contactRows: PanelRow[] =
        withDetail > 0 && !hasContacts
          ? []
          : [
              [t("field.orcid", lang), completionRow(withOrcid, withDetail)],
              [t("field.github", lang), completionRow(withGithub, withDetail)],
              [t("field.email", lang), completionRow(withEmail, withDetail)],
            ];

      return show(
        t("overview.title", lang),
        tabKind,
        untitled([
          [t("field.authorsCount", lang), String(authors.length)],
          [t("overview.itmoAuthors", lang), String(authors.length - external)],
          [t("overview.externalAuthors", lang), String(external)],
          [t("field.deptsCount", lang), String(data.departments.length)],
          [t("overview.avgPubsPerAuthor", lang), avgPubs],
          ...contactRows,
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
      const rows: PanelRow[] = [deptRow];
      if (node.kind === "author") {
        const general: PanelRow[] = [
          deptRow,
          [t("field.pubsCount", lang), String(node.pubs_count)],
        ];
        const privateRows: PanelRow[] = [];
        const service: PanelRow[] = [keyRow, kindRow];

        const authorDetail = authorDetails.get(node.key);
        if (authorDetail) {
          const nameRu = authorDetail.name_ru ?? "";
          const nameEn = authorDetail.name_en ?? "";
          const fullName = localize(nameRu, nameEn, lang);
          if (fullName) {
            title = fullName;
            const otherName = localize(nameEn, nameRu, lang);
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
          if (authorDetail.affiliations?.length) {
            privateRows.push([
              t("field.affiliations", lang),
              { kind: "list", items: affiliationItems(authorDetail.affiliations) },
            ]);
          }
          if (authorDetail.name_variants?.openalex.length) {
            privateRows.push([
              t("field.nameVariantsOpenalex", lang),
              { kind: "list", items: authorDetail.name_variants.openalex },
            ]);
          }
          if (authorDetail.name_variants?.orcid.length) {
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

        const connections: PanelRow[] = [];
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
          connections.push([t("tab.pubs", lang), { kind: "list", nodeKind: "pub", items: pubRefs }]);
        }

        const coauthors = topCoauthorKeys(node.key);
        if (coauthors.length > 0) {
          connections.unshift([
            t("field.topCoauthors", lang),
            { kind: "list", nodeKind: "author", items: entityRefsOf(coauthors, lang) },
          ]);
        }

        const authorRepos = authorRepoKeysOf(node.key);
        if (authorRepos.length > 0) {
          connections.push([
            t("tab.repos", lang),
            { kind: "list", nodeKind: "repo", items: entityRefsOf(authorRepos, lang) },
          ]);
        }

        return show(
          title,
          kindLabel(node.kind, lang),
          [
            { title: lang === "ru" ? "Связи автора" : "Author connections", rows: connections },
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
        if (repoDetails.has(node.key)) {
          const repoDetail = repoDetails.get(node.key);
          if (repoDetail?.url) rows.push([t("field.github", lang), [codeLink(repoDetail.url)]]);
          if (repoDetail?.description)
            rows.push([t("field.description", lang), repoDetail.description]);
          if (repoDetail?.owner_type)
            rows.push([t("field.ownerType", lang), repoDetail.owner_type]);
          if (repoDetail?.license) rows.push([t("field.license", lang), repoDetail.license]);
          if (repoDetail?.has_readme) rows.push([t("field.hasReadme", lang), "✓"]);
        } else {
          rows.push([t("field.loadingDetails", lang), LOADING]);
        }
        const reportLinks = reportLinksOf(node.key, lang);
        if (reportLinks.length > 0) rows.push([t("field.report", lang), reportLinks]);

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
        if (!detail) rows.push([t("field.loadingDetails", lang), LOADING]);
        if (detail?.doi) rows.push([t("field.doi", lang), [doiLink(detail.doi)]]);
        if (detail?.openalex_url)
          rows.push([t("field.openalexUrl", lang), [openalexUrlLink(detail.openalex_url)]]);
        if (detail?.type) rows.push([t("field.pubType", lang), detail.type]);
        if (detail && detail.fields.length > 0)
          rows.push([t("field.pubFields", lang), detail.fields.join(", ")]);
        if (detail && detail.funding.length > 0)
          rows.push([
            t("field.grants", lang),
            { kind: "list", items: detail.funding.map(fundingItem) },
          ]);
        if (detail?.abstract)
          rows.push([t("field.abstract", lang), { kind: "longText", text: detail.abstract }]);

        // A link to our own repo replaces code_url: our data is more reliable
        // than harvested links.
        const pubRepoKeys = pubRepoIndex.get(node.key) ?? [];
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

        const pubAuthorKeys = pubAuthors.get(node.key) ?? [];
        if (pubAuthorKeys.length > 0)
          rows.push([
            t("tab.authors", lang),
            { kind: "list", items: entityRefsOf(pubAuthorKeys, lang) },
          ]);
      }

      return show(
        title,
        kindLabel(node.kind, lang),
        [
          { title: t("section.general", lang), rows },
          { title: t("section.service", lang), rows: [keyRow, kindRow] },
        ],
        true,
        subtitle,
      );
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
        const shared = pubKeysByYear(
          (authorPubs.get(from.key) ?? []).filter((pub) =>
            (authorPubs.get(to.key) ?? []).includes(pub),
          ),
        );
        if (shared.length > 0)
          rows.push([
            t("field.sharedPubs", lang),
            { kind: "list", items: entityRefsOf(shared, lang) },
          ]);
      } else if (from.kind === "pub" && to.kind === "pub") {
        const shared = (pubAuthors.get(from.key) ?? []).filter((author) =>
          (pubAuthors.get(to.key) ?? []).includes(author),
        );
        if (shared.length > 0)
          rows.push([
            t("field.sharedAuthors", lang),
            { kind: "list", items: entityRefsOf(shared, lang) },
          ]);
      }

      return show(t("kind.edge", lang), t("kind.edge", lang), untitled(rows), true);
    }

    if (selection.kind === "grant") {
      const grant = grantIndex(pubDetails).get(selection.key);
      if (!grant) {
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
        grant.funder ? `${grant.funder} ${grant.name}` : grant.name,
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
  loading: () => HTMLElement;
  title: string;
  kind: string;
  sections: PanelSection[];
  lang: AppState["lang"];
  backLabel: string | null;
  onSelectRef: (selection: Selection) => void;
  onBack: () => void;
  subtitle?: string | null;
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
    const body = document.createElement("span");
    body.className = "panel-list__body";
    li.appendChild(body);
    if (typeof item === "string") {
      body.textContent = item;
      return li;
    }
    if (item.kind === "text") body.append(item.text);
    else body.appendChild(item.kind === "link" ? linkElement(item) : refElement(item));
    if (item.meta) {
      const meta = document.createElement("span");
      meta.className = "panel-list__meta";
      meta.textContent = item.meta;
      body.append(" ", meta);
    }
    return li;
  }

  function disclosureButton(update: (expanded: boolean) => void): HTMLButtonElement {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "panel-list__more";
    let expanded = false;

    function renderButton(): void {
      button.textContent = t(expanded ? "list.showLess" : "list.showMore", lang);
      button.setAttribute("aria-expanded", String(expanded));
    }

    button.addEventListener("click", () => {
      expanded = !expanded;
      update(expanded);
      renderButton();
    });
    renderButton();
    return button;
  }

  function listElement(value: PanelList): HTMLUListElement {
    const ul = document.createElement("ul");
    ul.className = "panel-list";
    if (value.nodeKind) ul.dataset.nodeKind = value.nodeKind;
    const limit = value.nodeKind ? 3 : PANEL_CONFIG.listLimit;
    ul.append(...value.items.slice(0, limit).map(listItemElement));
    if (value.items.length <= limit) return ul;

    const toggleItem = document.createElement("li");
    toggleItem.className = "panel-list__toggle";
    let extra: HTMLLIElement[] = [];
    toggleItem.append(disclosureButton((expanded) => {
      if (expanded) {
        extra = value.items.slice(limit).map(listItemElement);
        toggleItem.before(...extra);
      } else {
        for (const item of extra) item.remove();
        extra = [];
      }
    }));
    ul.append(toggleItem);
    return ul;
  }

  function longTextElement(value: PanelLongText): HTMLDivElement {
    const el = document.createElement("div");
    const words = value.text.split(/\s+/).filter(Boolean);
    const limit = PANEL_CONFIG.abstractWords;
    if (words.length <= limit) {
      el.textContent = value.text;
      return el;
    }

    const preview = `${words.slice(0, limit).join(" ")}…`;
    const text = document.createTextNode(preview);
    el.append(text, " ", disclosureButton((expanded) => {
      text.data = expanded ? value.text : preview;
    }));
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
        dd.appendChild(options.loading());
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

function buildBarChart(
  title: string,
  bars: { label: string; value: number }[],
): HTMLElement | null {
  if (bars.length === 0) return null;
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
