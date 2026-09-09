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
// SearchDetail и жил в contracts/search.ts — имя тянулось из старого
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

// Один пункт из OpenAlex/ORCID affiliation-истории автора (new_cache
// хранит это как JSON-текст на узле Person, new_generate его разбирает —
// см. new_generate/nodes.py::_parse_json_list). years — годы, за которые
// известна эта аффилиация; source — "openalex" или "orcid".
export interface Affiliation {
  name: string;
  ror: string;
  years: number[];
  source: string;
}

// Личные данные автора — отдельным файлом (authors-detail.json). Файл
// больше не режется по полям в зависимости от сборки (--public/--private
// новый_generate не знает, см. graph_builder.py) — приватность решается
// тем, в какую папку (public/private) этот файл физически попадает при
// деплое, не содержимым самого файла.
export interface AuthorDetail {
  key: string;
  // Единственное поле здесь с тегом "public" в new_cache/export.py — id
  // автора в OpenAlex, ссылка на его публичный профиль.
  openalex_id: string;
  // Полное имя на каждом языке — заголовок приватной карточки автора
  // (features/panels.ts), как только этот detail домержился; до этого
  // момента заголовок — сокращённая AuthorNode.label/label_en.
  name_ru: string;
  name_en: string;
  // Варианты написания имени за вычетом того, что уже показано как
  // заголовок (ни сокращённой подписи, ни name_ru/name_en) — раздельно по
  // источнику: openalex — то, что OpenAlex видел по разным публикациям
  // автора; orcid — имя, под которым автор сам просит его указывать, плюс
  // варианты, которые он сам зарегистрировал в своём профиле ORCID.
  name_variants: { openalex: string[]; orcid: string[] };
  degree: string;
  github: string;
  orcid: string;
  google_scholar: string;
  openreview: string;
  email: string;
  emails: string[];
  affiliations: Affiliation[];
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
  url: string;
  has_readme: boolean;
  license: string;
  // Логины GitHub контрибьюторов репозитория (не путать с
  // features/panels.ts::repoContributorsOf() — та строит список ИЗ НАШЕГО
  // графа, по repo_author_edges, этот же список — сырой, с самого GitHub).
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
  type: string;
  fields: string[];
  // Почти всегда пустые массивы на реальных данных сегодня (OpenAlex редко
  // отдаёт что-то непустое) — тип оставлен нестрогим (не описываем форму
  // элемента), пока не появится реальный непустой пример.
  funding: unknown[];
  versions: unknown[];
  openalex_url: string;
  abstract: string;
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
