// Формы данных, которые реально отдаёт new_generate/generate_data.py (build_graph_data()).
// Это зеркало Python-кода, а не желаемая форма — если генератор поменяет
// вывод, сначала правится этот файл, а уже потом код, который на него ссылается.
//
// AuthorNode/RepoNode/PubNode — это только "summary": то, что нужно
// нарисовать точку на карте и мгновенно переключать вкладки
// (graph-data.json). Расширенные поля (личные данные автора,
// описание/владелец репозитория, заголовок/DOI/код публикации) вынесены в
// отдельные AuthorDetail/RepoDetail/PubDetail — их new_generate пишет в
// отдельные *-detail.json, которые new_gui подгружает лениво, после карты.
// У публикаций это разделение было и раньше (PubDetail раньше назывался
// PubDetail и жил в contracts/search.ts — имя тянулось из старого
// graph-search.js/old_gui, хотя используется далеко не только поиском;
// переименовано, когда авторы/репозитории получили тот же принцип и старое
// имя стало явно вводить в заблуждение). contracts/search.ts остаётся
// только для того, что реально специфично поиску (SearchHit).

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
}

// Личные данные автора — отдельным файлом (authors-detail.json), которого
// вообще не существует в --public сборке (не отдельные поля вырезаны из
// объекта, как раньше, а целиком нет файла — надёжнее: новое личное поле
// физически не может утечь туда, где детали не публикуются).
export interface AuthorDetail {
  key: string;
  name_ru: string;
  // Варианты имени за вычетом того, что уже показано в label/label_en —
  // не сырой список из OpenAlex.
  name_variants: string[];
  degree: string;
  github: string;
  orcid: string;
}

export interface RepoNode {
  key: string;
  kind: "repo";
  dept: number;
  label: string;
  stars: number;
  rank: number;
  gx: number;
  gy: number;
}

export interface RepoDetail {
  key: string;
  description: string;
  owner: string;
  url: string;
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

// Заголовок/журнал/DOI/код публикации — раньше называлось PubDetail (см.
// докстринг файла выше про переименование). PubNode своего заголовка
// никогда не нёс: на карте публикация не подписана текстом, заголовок нужен
// только в карточке/списках/поиске — ровно то, для чего и есть detail-файл.
export interface PubDetail {
  key: string;
  label: string;
  journal: string;
  doi: string;
  has_code: boolean;
  code_url: string[];
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
