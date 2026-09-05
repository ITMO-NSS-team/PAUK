// Формы данных, которые реально отдаёт pauk/gui/generate_data.py (build_graph_data()).
// Это зеркало Python-кода, а не желаемая форма — если генератор поменяет
// вывод, сначала правится этот файл, а уже потом код, который на него ссылается.

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
}

export interface AuthorNode {
  key: string;
  kind: "author";
  dept: number;
  label: string;
  // Добавлено после LLM RU/EN разбора имён (коммит 65f7765) — присутствует
  // всегда, и в приватной, и в публичной сборке. Не то же самое, что name_en.
  label_en: string;
  pubs_count: number;
  rank: number;
  gx: number;
  gy: number;
  // Отсутствуют целиком в --public сборке. name_en среди них НЕТ —
  // англоязычное имя всегда видно через label_en выше.
  name_ru?: string;
  // Варианты имени за вычетом того, что уже показано в label/label_en —
  // не сырой список из OpenAlex.
  name_variants?: string[];
  degree?: string;
  github?: string;
  orcid?: string;
}

export interface RepoNode {
  key: string;
  kind: "repo";
  dept: number;
  label: string;
  description: string;
  stars: number;
  owner: string;
  url: string;
  rank: number;
  gx: number;
  gy: number;
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

export interface Edge {
  s: string;
  t: string;
  w: number;
}

// В отличие от Edge выше (s/t — строковые ключи author/pub/repo), у
// dept_edges s/t — это Department.id (сквозной числовой gid, см.
// generate_data.py: g() возвращает int, а не строку) — отдельный тип, а не
// переиспользование Edge, чтобы не смешивать два разных вида ключей.
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

// Пары {s, t} без веса — в отличие от Edge выше.
export interface UnweightedEdge {
  s: string;
  t: string;
}

export interface GraphData {
  departments: Department[];
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
