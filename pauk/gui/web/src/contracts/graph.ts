// Mirror of what pauk/gui/graph_builder writes. Change this file first when
// the builder output changes.
//
// *Node types are the map summary (graph-data.json); *Detail types come from
// *-detail.json, loaded lazily after the map.

export type NodeKind = "author" | "repo" | "pub";

export interface Department {
  id: number;
  name: string;
  name_en: string;
  color: string;
  n: number;
  n_authors: number;
  n_pubs: number;
  n_repos: number;
  // Search only. Missing in older graph-data.json.
  name_variants?: string[];
}

export interface AuthorNode {
  key: string;
  kind: "author";
  // false: external co-author. Missing in older data, where everyone is ITMO.
  is_itmo?: boolean;
  dept: number;
  label: string;
  label_en: string;
  pubs_count: number;
  rank: number;
  gx: number;
  gy: number;
}

export interface Affiliation {
  name: string;
  ror: string;
  years: number[];
  // "openalex" or "orcid".
  source: string;
}

// Personal data: written only to private/, never to public/.
export interface AuthorDetail {
  key: string;
  openalex_id: string;
  name_ru: string;
  name_en: string;
  // Spellings other than the ones already shown as the title.
  name_variants: { openalex: string[]; orcid: string[] };
  degree: string;
  github: string;
  orcid: string;
  google_scholar: string;
  email: string;
  affiliations: Affiliation[];
  // Optional: missing in older authors-detail.json. Keyed by pub key.
  pub_roles?: Record<string, PubRole>;
  // ISO strings from Neo4j.
  created_at?: string;
  updated_at?: string;
}

export interface PubRole {
  position: number | null;
  corresponding: boolean;
}

export interface RepoNode {
  key: string;
  kind: "repo";
  dept: number;
  // Department id or RepoGroup.id, colors the repos tab. Missing in older data.
  group?: number;
  label: string;
  stars: number;
  rank: number;
  gx: number;
  gy: number;
}

export interface RepoDetail {
  key: string;
  description: string;
  url: string;
  has_readme: boolean;
  license: string;
  // Raw GitHub logins, unlike repo_author_edges which link to our authors.
  contributors: string[];
  owner_type: string;
}

export interface PubNode {
  key: string;
  kind: "pub";
  dept: number;
  depts: number[];
  year: number | null;
  n_authors: number;
  rank: number;
  gx: number;
  gy: number;
}

export interface PubDetail {
  key: string;
  label: string;
  journal: string;
  doi: string;
  has_code: boolean;
  code_url: string[];
  type: string;
  fields: string[];
  funding: Funding[];
  versions: unknown[];
  openalex_url: string;
  abstract: string;
}

export interface Funding {
  funder: string;
  grant_id: string | null;
  // Normalized by graph_builder/grants.py; null when the number is missing or
  // cut short. Missing in older pubs-detail.json.
  grant_key?: string | null;
}

export interface Edge {
  s: string;
  t: string;
  w: number;
  // repo_edges only: which signals linked the pair.
  via?: RepoEdgeSignal[];
}

export type RepoEdgeSignal = "pub" | "person" | "coauthor" | "owner";

// Shaped like a department so regions, labels and selection need no special
// case. Ids never overlap Department.id.
export interface RepoGroup extends Department {
  kind: "org" | "field";
}

// s/t are Department.id, not node keys.
export interface DeptEdge {
  s: number;
  t: number;
  w: number;
}

export interface RepoAuthorEdge {
  s: string;
  t: string;
  role: string;
}

export interface UnweightedEdge {
  s: string;
  t: string;
}

export interface GraphData {
  departments: Department[];
  repo_groups?: RepoGroup[];
  dept_edges: DeptEdge[];
  authors: AuthorNode[];
  coauth_edges: Edge[];
  repos: RepoNode[];
  repo_edges: Edge[];
  repo_author_edges: RepoAuthorEdge[];
  repo_pub_edges: UnweightedEdge[];
  pubs: PubNode[];
  pub_edges: Edge[];
  all_edges: UnweightedEdge[];
}
