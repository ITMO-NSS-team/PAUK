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
  name_variants?: string[];
}

export interface AuthorNode {
  key: string;
  kind: "author";
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

export interface AuthorDetail {
  key: string;
  openalex_id: string;
  name_ru: string;
  name_en: string;
  name_variants: { openalex: string[]; orcid: string[] };
  degree: string;
  github: string;
  orcid: string;
  google_scholar: string;
  email: string;
  affiliations: Affiliation[];
  pub_roles?: Record<string, PubRole>;
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
  contributors: string[];
  owner_type: string;
}

export type ReportsIndex = Record<string, string[]>;

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
  sources?: string[];
  source_keys?: string[];
  source_links?: SourceLink[];
  openalex_url: string;
  abstract: string;
}

export interface SourceLink {
  source: string;
  key: string;
  url: string;
}

export interface Funding {
  funder: string;
  grant_id: string | null;
  grant_key?: string | null;
}

export interface Edge {
  s: string;
  t: string;
  w: number;
  via?: RepoEdgeSignal[];
}

export type RepoEdgeSignal = "pub" | "person" | "coauthor" | "owner";

export interface RepoGroup extends Department {
  kind: "org" | "field";
}

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
