import type {
  AuthorNode,
  Department,
  GraphData,
  PubDetail,
  PubNode,
  RepoAuthorEdge,
  RepoGroup,
  RepoNode,
} from "../contracts/graph";
import { localize, type Lang } from "./i18n";

type GraphNode = AuthorNode | RepoNode | PubNode;

/** Repos are colored by `group` (department, GitHub org or field), others by `dept`. */
export function groupIdOf(node: GraphNode): number {
  return node.kind === "repo" ? (node.group ?? node.dept) : node.dept;
}

/** Departments and repo groups in one index; their ids never overlap. */
export function groupsById(data: GraphData): Map<number, Department | RepoGroup> {
  return new Map<number, Department | RepoGroup>(
    [...data.departments, ...(data.repo_groups ?? [])].map((group) => [group.id, group]),
  );
}

/** Checks the shape in dev mode only. */
export async function loadGraphData(url: string): Promise<GraphData> {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`failed to load ${url}: HTTP ${response.status}`);
  }

  const data = (await response.json()) as GraphData;
  if (import.meta.env.DEV) assertGraphData(data);
  return data;
}

/** Throws on a missing file; app/main.ts catches it (e.g. no authors-detail.json in public/). */
export async function loadDetails<T>(url: string): Promise<T[]> {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`failed to load ${url}: HTTP ${response.status}`);
  }
  return (await response.json()) as T[];
}

export function indexDetailsByKey<T extends { key: string }>(details: T[]): Map<string, T> {
  return new Map(details.map((detail) => [detail.key, detail]));
}

/**
 * Merges into the existing map in place: features already hold this map and
 * only need `store.notify()` afterwards.
 */
export function mergeDetailsInto<T extends { key: string }>(target: Map<string, T>, details: T[]): void {
  for (const detail of details) target.set(detail.key, detail);
}

/**
 * Dev-only sanity check against contract drift with the Python builder, not
 * a full schema validation: the data comes from our own builder.
 */
export function assertGraphData(data: unknown): asserts data is GraphData {
  if (typeof data !== "object" || data === null) {
    throw new Error("assertGraphData: expected an object");
  }

  const graph = data as Record<string, unknown>;
  const requiredArrayKeys: (keyof GraphData)[] = [
    "departments",
    "authors",
    "repos",
    "pubs",
    "coauth_edges",
    "repo_edges",
    "pub_edges",
  ];
  for (const key of requiredArrayKeys) {
    if (!Array.isArray(graph[key])) {
      throw new Error(`assertGraphData: field "${key}" is missing or not an array`);
    }
  }

  const firstAuthor = (graph.authors as unknown[])[0] as Record<string, unknown> | undefined;
  if (firstAuthor && (typeof firstAuthor.label_en !== "string" || typeof firstAuthor.key !== "string")) {
    throw new Error(
      "assertGraphData: AuthorNode no longer matches the contract (missing key/label_en); check builder.py",
    );
  }
}

export function indexByKey(data: GraphData): Map<string, GraphNode> {
  const index = new Map<string, GraphNode>();
  for (const node of [...data.authors, ...data.repos, ...data.pubs]) {
    index.set(node.key, node);
  }
  return index;
}

/** Author <-> pub lists from `all_edges`, the only direct link between them. */
export function buildAuthorPubIndex(data: GraphData): {
  authorPubs: Map<string, string[]>;
  pubAuthors: Map<string, string[]>;
} {
  const authorPubs = new Map<string, string[]>();
  const pubAuthors = new Map<string, string[]>();

  for (const { s, t } of data.all_edges) {
    const pubsOfAuthor = authorPubs.get(s) ?? [];
    pubsOfAuthor.push(t);
    authorPubs.set(s, pubsOfAuthor);

    const authorsOfPub = pubAuthors.get(t) ?? [];
    authorsOfPub.push(s);
    pubAuthors.set(t, authorsOfPub);
  }

  return { authorPubs, pubAuthors };
}

/** Author -> co-author -> shared pub count. Edges are undirected. */
export function buildCoauthIndex(data: GraphData): Map<string, Map<string, number>> {
  const index = new Map<string, Map<string, number>>();

  function addWeight(from: string, to: string, weight: number): void {
    const neighbors = index.get(from) ?? new Map<string, number>();
    neighbors.set(to, (neighbors.get(to) ?? 0) + weight);
    index.set(from, neighbors);
  }

  for (const edge of data.coauth_edges) {
    addWeight(edge.s, edge.t, edge.w);
    addWeight(edge.t, edge.s, edge.w);
  }

  return index;
}

/** Same as {@link buildCoauthIndex}, keyed by department id. */
export function buildDeptEdgeIndex(data: GraphData): Map<number, Map<number, number>> {
  const index = new Map<number, Map<number, number>>();

  function addWeight(from: number, to: number, weight: number): void {
    const neighbors = index.get(from) ?? new Map<number, number>();
    neighbors.set(to, (neighbors.get(to) ?? 0) + weight);
    index.set(from, neighbors);
  }

  for (const edge of data.dept_edges) {
    addWeight(edge.s, edge.t, edge.w);
    addWeight(edge.t, edge.s, edge.w);
  }

  return index;
}

/** Direct contributor links only, not repos linked through the author's pubs. */
export function buildAuthorRepoIndex(data: GraphData): Map<string, string[]> {
  const index = new Map<string, string[]>();
  for (const edge of data.repo_author_edges) {
    const repos = index.get(edge.t) ?? [];
    repos.push(edge.s);
    index.set(edge.t, repos);
  }
  return index;
}

export function buildRepoAuthorIndex(data: GraphData): Map<string, RepoAuthorEdge[]> {
  const index = new Map<string, RepoAuthorEdge[]>();
  for (const edge of data.repo_author_edges) {
    const members = index.get(edge.s) ?? [];
    members.push(edge);
    index.set(edge.s, members);
  }
  return index;
}

export function buildRepoPubIndex(data: GraphData): { repoPubs: Map<string, string[]>; pubRepos: Map<string, string[]> } {
  const repoPubs = new Map<string, string[]>();
  const pubRepos = new Map<string, string[]>();

  for (const { s, t } of data.repo_pub_edges) {
    const pubsOfRepo = repoPubs.get(s) ?? [];
    pubsOfRepo.push(t);
    repoPubs.set(s, pubsOfRepo);

    const reposOfPub = pubRepos.get(t) ?? [];
    reposOfPub.push(s);
    pubRepos.set(t, reposOfPub);
  }

  return { repoPubs, pubRepos };
}

const GITHUB_URL_PREFIX = "https://github.com/";

/** `https://github.com/owner/repo` -> `owner/repo`; other strings pass through. */
export function githubShortPath(url: string): string {
  return url.replace(GITHUB_URL_PREFIX, "");
}

export function githubProfileUrl(username: string): string {
  return `${GITHUB_URL_PREFIX}${username}`;
}

/** PubNode has no label: the title comes from `pubDetails`, falling back to the key. */
export function nodeLabel(node: GraphNode, lang: Lang, pubDetails?: Map<string, PubDetail>): string {
  if (!("label" in node)) return pubDetails?.get(node.key)?.label ?? node.key;
  const labelEn = "label_en" in node ? node.label_en : undefined;
  return localize(node.label, labelEn, lang);
}

export interface GrantInfo {
  key: string;
  /** The spelling authors used most often. */
  name: string;
  funder: string;
  pubs: string[];
}

interface GrantAcc {
  pubs: Set<string>;
  names: Map<string, number>;
  funders: Map<string, number>;
}
const grantIndexCache = new WeakMap<
  Map<string, PubDetail>,
  { size: number; index: Map<string, GrantInfo> }
>();

/** Recomputed only when the detail map grows (it is filled in the background). */
export function grantIndex(pubDetails: Map<string, PubDetail>): Map<string, GrantInfo> {
  const cached = grantIndexCache.get(pubDetails);
  if (cached?.size === pubDetails.size) return cached.index;

  const acc = new Map<string, GrantAcc>();
  for (const detail of pubDetails.values()) {
    for (const entry of detail.funding) {
      if (!entry.grant_key) continue;
      const grant = acc.get(entry.grant_key) ?? {
        pubs: new Set<string>(),
        names: new Map<string, number>(),
        funders: new Map<string, number>(),
      };
      grant.pubs.add(detail.key);
      const name = entry.grant_id ?? entry.grant_key;
      grant.names.set(name, (grant.names.get(name) ?? 0) + 1);
      if (entry.funder) grant.funders.set(entry.funder, (grant.funders.get(entry.funder) ?? 0) + 1);
      acc.set(entry.grant_key, grant);
    }
  }
  const mostCommon = (counts: Map<string, number>): string =>
    [...counts].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))[0]?.[0] ?? "";
  const index = new Map<string, GrantInfo>();
  for (const [key, grant] of acc) {
    index.set(key, {
      key,
      name: mostCommon(grant.names).trim(),
      funder: mostCommon(grant.funders),
      pubs: [...grant.pubs],
    });
  }
  grantIndexCache.set(pubDetails, { size: pubDetails.size, index });
  return index;
}

/** RFC 4180 with a BOM, so Excel reads Cyrillic as UTF-8. */
export function toCsv(rows: string[][]): string {
  const cell = (value: string): string =>
    /[",\r\n]/.test(value) ? `"${value.replaceAll('"', '""')}"` : value;
  return "\uFEFF" + rows.map((row) => row.map(cell).join(",")).join("\r\n");
}
