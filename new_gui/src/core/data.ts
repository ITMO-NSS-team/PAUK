import type { AuthorNode, GraphData, PubNode, RepoAuthorEdge, RepoNode } from "../contracts/graph";
import type { SearchDetail } from "../contracts/search";
import { localize, type Lang } from "./i18n";
import sampleGraphData from "./fixtures/graph-data.sample.json";
import sampleSearchDetails from "./fixtures/graph-search.sample.json";

/** Любой из трёх видов узлов графа — авторы, репозитории, публикации. */
type GraphNode = AuthorNode | RepoNode | PubNode;

/**
 * Старый и новый GUI работают с одними и теми же файлами, которые генерирует
 * Python (pauk/gui/generate_data.py) — трогать генератор нам сейчас нельзя,
 * поэтому new_gui сам разбирает существующий формат на клиенте: файл — это
 * не JSON, а присваивание вида `window.GRAPH={...};`, рассчитанное на
 * подключение через <script>. Отрезаем известные "обвязки" по краям и
 * парсим то, что осталось, как обычный JSON.
 */
/** Чистая функция без сети — вынесена отдельно, чтобы проверять её тестом на реальном файле. */
export function parseWrappedJson<T>(text: string, prefix: string, suffix: string): T {
  if (!text.startsWith(prefix) || !text.endsWith(suffix)) {
    throw new Error(`неожиданный формат файла данных: не начинается с "${prefix}" или не кончается на "${suffix}"`);
  }

  const json = suffix.length > 0 ? text.slice(prefix.length, -suffix.length) : text.slice(prefix.length);
  return JSON.parse(json) as T;
}

/**
 * Загрузка настоящего graph-data.js (легаси-формат window.GRAPH=...).
 * Пока не используется: v2-прототип временно работает на синтетическом
 * фикстур-наборе (см. loadSampleGraphData ниже) — реальные данные
 * подключим отдельным шагом, когда дойдём до интеграции с генератором.
 */
export async function loadGraphData(url: string): Promise<GraphData> {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`не удалось загрузить ${url}: HTTP ${response.status}`);
  }

  const data = parseWrappedJson<GraphData>(await response.text(), "window.GRAPH=", "");
  if (import.meta.env.DEV) assertGraphData(data);
  return data;
}

/**
 * Синтетические данные для разработки v2-прототипа — небольшой, но полный
 * набор (департаменты, авторы, репозитории, публикации, все виды рёбер),
 * который сам соответствует контракту. Реальный pauk/gui/data сейчас не
 * трогаем и на него не полагаемся.
 */
export async function loadSampleGraphData(): Promise<GraphData> {
  const data = sampleGraphData as GraphData;
  if (import.meta.env.DEV) assertGraphData(data);
  return data;
}

/**
 * Синтетический аналог graph-search.js — детали публикаций (настоящее
 * название, журнал, DOI, ссылка на код), которых нет в самом GraphData.
 * Соответствует по ключам публикациям из loadSampleGraphData() (P1-P6) —
 * реальный graph-search.js подключим тем же следующим шагом, что и
 * graph-data.js (см. loadGraphData выше).
 */
export async function loadSampleSearchDetails(): Promise<SearchDetail[]> {
  return sampleSearchDetails as SearchDetail[];
}

/** Быстрый поиск деталей публикации по ключу — тот же принцип, что и indexByKey() ниже. */
export function indexSearchDetailsByKey(details: SearchDetail[]): Map<string, SearchDetail> {
  return new Map(details.map((detail) => [detail.key, detail]));
}

/**
 * Лёгкая проверка формы, которая падает только в dev-режиме
 * (import.meta.env.DEV) — источник данных доверенный (свой генератор, не
 * пользовательский ввод), поэтому вместо zod/valibot держим один быстрый
 * тест на рассинхрон контракта с Python, а не полноценную рантайм-схему.
 */
export function assertGraphData(data: unknown): asserts data is GraphData {
  if (typeof data !== "object" || data === null) {
    throw new Error("assertGraphData: ожидался объект");
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
      throw new Error(`assertGraphData: поле "${key}" отсутствует или не массив`);
    }
  }

  const firstAuthor = (graph.authors as unknown[])[0] as Record<string, unknown> | undefined;
  if (firstAuthor && (typeof firstAuthor.label_en !== "string" || typeof firstAuthor.key !== "string")) {
    throw new Error(
      "assertGraphData: форма AuthorNode разошлась с контрактом (нет key/label_en) — проверь generate_data.py",
    );
  }
}

/**
 * Быстрый поиск узла по его ключу (например "A5133538481" для автора или
 * "W7164652155" для публикации) — без этой карты пришлось бы каждый раз
 * перебирать три массива (authors/repos/pubs) целиком. Строится один раз
 * при монтировании фичи (клика по карте, панели информации и т.д.), не на
 * каждый клик — иначе на большом графе это было бы заметно медленно.
 */
export function indexByKey(data: GraphData): Map<string, GraphNode> {
  const index = new Map<string, GraphNode>();
  for (const node of [...data.authors, ...data.repos, ...data.pubs]) {
    index.set(node.key, node);
  }
  return index;
}

/**
 * Обратные индексы автор <-> публикация, построенные из GraphData.all_edges —
 * единственного места в контракте, где вообще есть такая связь напрямую
 * (author.pubs_count — просто число, а не список ключей). Нужны для карточки
 * ребра (features/panels.ts): "какие именно публикации общие у пары
 * соавторов" / "какие именно авторы общие у пары связанных публикаций" —
 * то, что сам вес ребра (coauth_edges.w / pub_edges.w) только считает, не называя.
 */
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

/**
 * Соавторство одного автора со всеми остальными: ключ соавтора -> суммарный
 * вес (число совместных публикаций). coauth_edges неориентированы (нет
 * отдельной записи в обе стороны), поэтому при обходе учитываем ребро с
 * обоих концов. Нужен карточке автора (features/panels.ts) для топа
 * соавторов — просто node.pubs_count этого не показывает, он только считает,
 * не называет.
 */
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

/**
 * Связи между департаментами (общие публикации через их авторов) — та же
 * идея, что и buildCoauthIndex, только ключи не строковые (author.key), а
 * числовые (Department.id, см. DeptEdge в contracts/graph.ts). Нужен
 * карточке департамента (features/panels.ts) для списка "связанные
 * департаменты", отсортированного по силе связи.
 */
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

/**
 * Автор -> ключи репозиториев, где он указан контрибьютором, из
 * GraphData.repo_author_edges (s — репозиторий, t — автор — см. контракт
 * RepoAuthorEdge). Только прямая связь, без учёта "репозиторий связан с
 * публикацией автора" через repo_pub_edges — так делал старый GUI
 * (authorRepos), но это отдельный, более сложный источник той же
 * информации; пока хватает прямой связи.
 */
export function buildAuthorRepoIndex(data: GraphData): Map<string, string[]> {
  const index = new Map<string, string[]>();
  for (const edge of data.repo_author_edges) {
    const repos = index.get(edge.t) ?? [];
    repos.push(edge.s);
    index.set(edge.t, repos);
  }
  return index;
}

/**
 * Репозиторий -> участники (с ролью), из GraphData.repo_author_edges —
 * обратная сторона buildAuthorRepoIndex (там ключ — автор, здесь — сам
 * репозиторий). Нужен карточке репозитория (features/panels.ts): "кто
 * именно над ним работал", а не только владелец из RepoNode.owner.
 */
export function buildRepoAuthorIndex(data: GraphData): Map<string, RepoAuthorEdge[]> {
  const index = new Map<string, RepoAuthorEdge[]>();
  for (const edge of data.repo_author_edges) {
    const members = index.get(edge.s) ?? [];
    members.push(edge);
    index.set(edge.s, members);
  }
  return index;
}

/**
 * Репозиторий -> ключи публикаций, из GraphData.repo_pub_edges — последнее
 * из четырёх "неиспользуемых" полей контракта (all_edges, repo_author_edges,
 * dept_edges — уже задействованы). Нужен карточке репозитория: "какие
 * публикации с ним связаны".
 */
export function buildRepoPubIndex(data: GraphData): Map<string, string[]> {
  const index = new Map<string, string[]>();
  for (const edge of data.repo_pub_edges) {
    const pubs = index.get(edge.s) ?? [];
    pubs.push(edge.t);
    index.set(edge.s, pubs);
  }
  return index;
}

/**
 * Подпись узла для интерфейса, на нужном языке. У PubNode своего label
 * нет вообще — заголовок публикации приходит отдельно, из SearchDetail
 * (graph-search.js), а не из самого узла графа (см. contracts/graph.ts).
 * searchDetails — необязательный параметр: если для публикации нашлось
 * название, используем его; если карта не передана или в ней нет такого
 * ключа, откатываемся на key как и раньше — так вызывающему коду, у
 * которого ещё нет доступа к SearchDetail, не обязательно ничего менять.
 *
 * У AuthorNode есть пара label/label_en — переключаем через localize().
 * У RepoNode своего _en варианта нет (имя репозитория не переводится),
 * поэтому для него localize() просто вернёт repo.label на любом языке.
 * У SearchDetail тоже нет _en-варианта (реальный graph-search.js его не
 * содержит) — название публикации всегда на одном языке, независимо от lang.
 */
export function nodeLabel(node: GraphNode, lang: Lang, searchDetails?: Map<string, SearchDetail>): string {
  if (!("label" in node)) return searchDetails?.get(node.key)?.label ?? node.key;
  const labelEn = "label_en" in node ? node.label_en : undefined;
  return localize(node.label, labelEn, lang);
}
