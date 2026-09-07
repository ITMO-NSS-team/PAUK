import type {
  AuthorDetail,
  AuthorNode,
  GraphData,
  PubDetail,
  PubNode,
  RepoAuthorEdge,
  RepoDetail,
  RepoNode,
} from "../contracts/graph";
import { localize, type Lang } from "./i18n";
import sampleGraphData from "./fixtures/graph-data.sample.json";
import sampleAuthorDetails from "./fixtures/authors-detail.sample.json";
import sampleRepoDetails from "./fixtures/repos-detail.sample.json";
import samplePubDetails from "./fixtures/pubs-detail.sample.json";

/** Любой из трёх видов узлов графа — авторы, репозитории, публикации. */
type GraphNode = AuthorNode | RepoNode | PubNode;

/**
 * Загружает `graph-data.json` по сети и проверяет его форму в dev-режиме
 * через {@link assertGraphData}. Голый JSON, без обёртки `window.GRAPH=...;`
 * — та обёртка была нужна только старому `pauk/gui/web/` (подключение через
 * `<script>` без сборщика), `new_generate/generate_data.py` пишет обычный
 * `.json`, поэтому здесь просто `response.json()`.
 *
 * Пока не используется нигде в приложении: v2-прототип временно работает
 * на синтетическом фикстур-наборе (см. {@link loadSampleGraphData} ниже) —
 * реальные данные подключим отдельным шагом, когда дойдём до интеграции с
 * генератором.
 *
 * @param url - адрес файла `graph-data.json` (например, из Vite dev-сервера прокси или статики).
 * @returns Промис с данными графа.
 * @throws Error, если HTTP-запрос не удался (`!response.ok`).
 */
export async function loadGraphData(url: string): Promise<GraphData> {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`не удалось загрузить ${url}: HTTP ${response.status}`);
  }

  const data = (await response.json()) as GraphData;
  if (import.meta.env.DEV) assertGraphData(data);
  return data;
}

/**
 * Загружает синтетические данные для разработки v2-прототипа — небольшой,
 * но полный набор (департаменты, авторы, репозитории, публикации, все виды
 * рёбер), который сам соответствует контракту {@link GraphData}. Реальный
 * `pauk/gui/data` сейчас не трогаем и на него не полагаемся — см.
 * `src/core/fixtures/graph-data.sample.json`.
 *
 * @returns Промис с фикстур-данными (асинхронность — только ради единого
 *   интерфейса с {@link loadGraphData}, сам импорт JSON синхронный).
 */
export async function loadSampleGraphData(): Promise<GraphData> {
  const data = sampleGraphData as GraphData;
  if (import.meta.env.DEV) assertGraphData(data);
  return data;
}

/**
 * Загружает синтетический аналог `pubs-detail.json` — детали публикаций
 * (настоящее название, журнал, DOI, ссылка на код), которых нет в самом
 * `GraphData`. Соответствует по ключам публикациям из
 * {@link loadSampleGraphData} (P1-P6) — реальный `pubs-detail.json`
 * подключим тем же следующим шагом, что и `graph-data.json` (см.
 * {@link loadGraphData}).
 *
 * @returns Промис со списком деталей публикаций.
 */
export async function loadSamplePubDetails(): Promise<PubDetail[]> {
  return samplePubDetails as PubDetail[];
}

/**
 * Загружает синтетический аналог `authors-detail.json` — личные данные
 * авторов (ФИО целиком, варианты имени, степень, GitHub, ORCID), которых
 * больше нет в самом `AuthorNode` (см. {@link AuthorDetail} в
 * contracts/graph.ts). Каждый автор из {@link loadSampleGraphData} имеет
 * запись здесь (даже если все поля пустые) — `new_generate` строит этот
 * файл на каждого автора без исключения, за вычетом самой `--public`
 * сборки, у которой этого файла нет вовсе.
 *
 * @returns Промис со списком деталей авторов.
 */
export async function loadSampleAuthorDetails(): Promise<AuthorDetail[]> {
  return sampleAuthorDetails as AuthorDetail[];
}

/**
 * Загружает синтетический аналог `repos-detail.json` — описание, владелец
 * и ссылка репозитория, которых больше нет в самом `RepoNode` (см.
 * {@link RepoDetail} в contracts/graph.ts).
 *
 * @returns Промис со списком деталей репозиториев.
 */
export async function loadSampleRepoDetails(): Promise<RepoDetail[]> {
  return sampleRepoDetails as RepoDetail[];
}

/**
 * Строит индекс "ключ -> сам объект" по списку любых деталей (авторов,
 * репозиториев или публикаций) — единая функция вместо трёх одинаковых по
 * смыслу копий, по одной на каждый вид `*Detail`. Тот же принцип, что и
 * {@link indexByKey} ниже, только для detail-объектов, а не узлов графа.
 *
 * @typeParam T - вид детали (в приложении — {@link AuthorDetail}, {@link RepoDetail} или `PubDetail`).
 * @param details - список деталей (например, результат {@link loadSampleAuthorDetails}).
 * @returns Map от `T.key` к самому объекту `T`.
 *
 * @example
 * const byKey = indexDetailsByKey([{ key: "P1", label: "...", ... }]);
 * byKey.get("P1"); // { key: "P1", label: "...", ... }
 * byKey.get("P2"); // undefined — такого ключа не было в списке
 */
export function indexDetailsByKey<T extends { key: string }>(details: T[]): Map<string, T> {
  return new Map(details.map((detail) => [detail.key, detail]));
}

/**
 * Домешивает список деталей в УЖЕ СУЩЕСТВУЮЩУЮ карту, по той же ссылке —
 * не создаёт новую `Map`, в отличие от {@link indexDetailsByKey}. Ровно то,
 * что нужно для приоритетной загрузки в `app/main.ts`: карта передаётся во
 * все фичи один раз, ещё пустой, а detail-файл домешивается в неё позже,
 * когда придёт по сети — фичам не нужно ничего пересоздавать или получать
 * заново, они уже держат ссылку на этот же объект.
 *
 * @typeParam T - вид детали (см. {@link indexDetailsByKey}).
 * @param target - карта, в которую нужно добавить записи (мутируется на месте).
 * @param details - список деталей, которые нужно добавить/перезаписать.
 *
 * @example
 * const authorDetails = new Map<string, AuthorDetail>(); // пока пуст
 * mountPanel(store, data, pubDetails, authorDetails, repoDetails); // уже держит эту ссылку
 * // ...позже:
 * mergeDetailsInto(authorDetails, await loadSampleAuthorDetails());
 * store.notify(); // mountPanel перечитывает ту же authorDetails и видит новые записи
 */
export function mergeDetailsInto<T extends { key: string }>(target: Map<string, T>, details: T[]): void {
  for (const detail of details) target.set(detail.key, detail);
}

/**
 * Лёгкая проверка формы данных, которая падает только в dev-режиме
 * (`import.meta.env.DEV`) — источник данных доверенный (свой генератор, не
 * пользовательский ввод), поэтому вместо полноценной рантайм-схемы
 * (zod/valibot) здесь один быстрый тест на рассинхрон контракта с Python,
 * а не валидация каждого поля.
 *
 * @param data - произвольное значение, которое должно быть {@link GraphData}
 *   (обычно результат `JSON.parse` или импорта фикстуры).
 * @throws Error с понятным текстом, если:
 *   - `data` не объект;
 *   - у одного из обязательных полей (`departments`, `authors`, `repos`,
 *     `pubs`, `coauth_edges`, `repo_edges`, `pub_edges`) нет массива;
 *   - у первого автора нет `key`/`label_en` нужного типа (признак того, что
 *     `generate_data.py` поменял форму `AuthorNode`).
 *
 * Ничего не делает и не бросает исключений, если данные прошли все
 * проверки — используется как type assertion (`asserts data is GraphData`),
 * поэтому TypeScript после вызова знает, что `data` имеет тип `GraphData`,
 * без отдельного приведения через `as`.
 *
 * @example
 * const data: unknown = JSON.parse(text);
 * assertGraphData(data);
 * // здесь TypeScript уже считает data: GraphData, можно писать data.authors
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
 * Строит индекс "ключ узла -> сам узел" по всем трём видам узлов графа
 * сразу (авторы, репозитории, публикации). Без этой карты пришлось бы
 * каждый раз перебирать три массива целиком, чтобы найти один узел по
 * ключу (например, `"A5133538481"` для автора или `"W7164652155"` для
 * публикации).
 *
 * Строится один раз при монтировании фичи (клика по карте, панели
 * информации и т.д.), не на каждый клик — иначе на большом графе
 * пересборка индекса на каждое действие пользователя была бы заметно
 * медленной.
 *
 * @param data - данные графа.
 * @returns Map от `node.key` к самому узлу (`AuthorNode | RepoNode | PubNode`).
 *
 * @example
 * const index = indexByKey(data);
 * index.get("A1"); // { key: "A1", kind: "author", label: "Иванов И.И.", ... }
 * index.get("nope"); // undefined
 */
export function indexByKey(data: GraphData): Map<string, GraphNode> {
  const index = new Map<string, GraphNode>();
  for (const node of [...data.authors, ...data.repos, ...data.pubs]) {
    index.set(node.key, node);
  }
  return index;
}

/**
 * Строит обратные индексы автор ↔ публикация из `GraphData.all_edges` —
 * единственного места в контракте, где эта связь вообще есть напрямую
 * (`AuthorNode.pubs_count` — просто число, а не список ключей публикаций).
 *
 * Нужны карточке ребра и карточкам узлов (features/panels.ts): "какие
 * именно публикации общие у пары соавторов", "какие именно авторы общие у
 * пары связанных публикаций", "какие публикации у этого автора", "кто
 * авторы этой публикации" — то, что сам вес ребра (`coauth_edges.w` /
 * `pub_edges.w`) или `pubs_count` только считает, не называя.
 *
 * @param data - данные графа.
 * @returns Объект с двумя картами:
 *   - `authorPubs` — ключ автора -> список ключей его публикаций;
 *   - `pubAuthors` — ключ публикации -> список ключей её авторов.
 *
 * @example
 * // all_edges: [{s:"A1",t:"P1"}, {s:"A2",t:"P1"}]
 * const { authorPubs, pubAuthors } = buildAuthorPubIndex(data);
 * authorPubs.get("A1"); // ["P1"]
 * pubAuthors.get("P1"); // ["A1", "A2"]
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
 * Строит индекс соавторства: для каждого автора — карта "ключ соавтора ->
 * суммарный вес" (суммарное число совместных публикаций). `coauth_edges`
 * неориентированы (нет отдельной записи в обе стороны для одной и той же
 * пары), поэтому при обходе каждое ребро учитывается с обоих концов.
 *
 * Нужен карточке автора (features/panels.ts) для топа соавторов — просто
 * `AuthorNode.pubs_count` этого не показывает, он только считает публикации,
 * не называет, с кем именно автор их писал.
 *
 * @param data - данные графа.
 * @returns Map от ключа автора к Map "ключ соавтора -> суммарный вес связи".
 *
 * @example
 * // coauth_edges: [{s:"A1",t:"A2",w:2}, {s:"A1",t:"A3",w:1}]
 * const index = buildCoauthIndex(data);
 * index.get("A1"); // Map { "A2" => 2, "A3" => 1 }
 * index.get("A2"); // Map { "A1" => 2 } — та же связь видна и со стороны A2
 */
export function buildCoauthIndex(data: GraphData): Map<string, Map<string, number>> {
  const index = new Map<string, Map<string, number>>();

  /**
   * Прибавляет `weight` к весу связи `from -> to` в `index` (создавая
   * запись, если её ещё не было). Вызывается дважды на каждое ребро —
   * `coauth_edges` неориентированы, поэтому связь должна быть видна с
   * обеих сторон.
   *
   * @param from - ключ автора, со стороны которого добавляется связь.
   * @param to - ключ соавтора на другом конце связи.
   * @param weight - вес, который нужно прибавить.
   */
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
 * Строит индекс связей между департаментами (общие публикации через их
 * авторов) — та же идея и то же устройство, что и {@link buildCoauthIndex},
 * только ключи не строковые (`author.key`), а числовые (`Department.id`,
 * см. `DeptEdge` в contracts/graph.ts).
 *
 * Нужен карточке департамента (features/panels.ts) для списка "связанные
 * департаменты", отсортированного по силе связи.
 *
 * @param data - данные графа.
 * @returns Map от id департамента к Map "id соседнего департамента -> суммарный вес связи".
 *
 * @example
 * // dept_edges: [{s:0,t:1,w:2}, {s:1,t:2,w:1}]
 * const index = buildDeptEdgeIndex(data);
 * index.get(0); // Map { 1 => 2 }
 * index.get(1); // Map { 0 => 2, 2 => 1 }
 */
export function buildDeptEdgeIndex(data: GraphData): Map<number, Map<number, number>> {
  const index = new Map<number, Map<number, number>>();

  /**
   * Прибавляет `weight` к весу связи `from -> to` в `index` (создавая
   * запись, если её ещё не было). Вызывается дважды на каждое ребро —
   * `dept_edges` неориентированы, поэтому связь должна быть видна с обеих сторон.
   *
   * @param from - id департамента, со стороны которого добавляется связь.
   * @param to - id соседнего департамента на другом конце связи.
   * @param weight - вес, который нужно прибавить.
   */
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
 * Строит индекс "автор -> ключи репозиториев, где он указан контрибьютором"
 * из `GraphData.repo_author_edges` (`s` — репозиторий, `t` — автор, см.
 * контракт `RepoAuthorEdge`).
 *
 * Учитывает только прямую связь автор-репозиторий, без учёта "репозиторий
 * связан с публикацией автора" через `repo_pub_edges` — так делал старый
 * GUI (`authorRepos`), но это отдельный, более сложный источник той же
 * информации; пока хватает прямой связи.
 *
 * @param data - данные графа.
 * @returns Map от ключа автора к списку ключей репозиториев.
 *
 * @example
 * // repo_author_edges: [{s:"R1",t:"A1",role:"maintainer"}]
 * buildAuthorRepoIndex(data).get("A1"); // ["R1"]
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
 * Строит индекс "репозиторий -> его участники (с ролью)" из
 * `GraphData.repo_author_edges` — обратная сторона
 * {@link buildAuthorRepoIndex} (там ключ — автор, здесь — сам репозиторий).
 *
 * Нужен карточке репозитория (features/panels.ts): "кто именно над ним
 * работал", а не только владелец из `RepoDetail.owner`.
 *
 * @param data - данные графа.
 * @returns Map от ключа репозитория к списку рёбер `RepoAuthorEdge`
 *   (каждое содержит ключ автора в поле `t` и его роль в поле `role`).
 *
 * @example
 * // repo_author_edges: [{s:"R1",t:"A1",role:"maintainer"}]
 * buildRepoAuthorIndex(data).get("R1"); // [{ s: "R1", t: "A1", role: "maintainer" }]
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
 * Строит обратные индексы репозиторий ↔ публикация из
 * `GraphData.repo_pub_edges` — тем же приёмом, что и {@link buildAuthorPubIndex}.
 *
 * Нужны карточке репозитория (features/panels.ts): "какие публикации с ним
 * связаны", и карточке публикации: "в каком репозитории её код" — в
 * старом GUI (`tab-pubs.js::showPubCard`) при наличии связанного репозитория
 * его ссылка показывалась ВМЕСТО голого `code_url` из `PubDetail` —
 * связь через собственные данные надёжнее, чем внешний харвестинг.
 *
 * @param data - данные графа.
 * @returns Объект с двумя картами:
 *   - `repoPubs` — ключ репозитория -> список ключей связанных публикаций;
 *   - `pubRepos` — ключ публикации -> список ключей связанных репозиториев.
 *
 * @example
 * // repo_pub_edges: [{s:"R1",t:"P1"}]
 * const { repoPubs, pubRepos } = buildRepoPubIndex(data);
 * repoPubs.get("R1"); // ["P1"]
 * pubRepos.get("P1"); // ["R1"]
 */
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

// Общий префикс GitHub-ссылок — используется и для сборки ссылки на профиль
// автора (githubLink в features/panels.ts), и здесь, для укорачивания уже
// готовой ссылки на репозиторий/код при отображении. Раньше строка
// "https://github.com/" была продублирована как литерал в трёх местах
// (features/panels.ts дважды, features/search/index.ts один раз) — общий
// источник правды означает, что при смене домена (например, на self-hosted
// GitHub Enterprise) достаточно поменять его в одном месте.
const GITHUB_URL_PREFIX = "https://github.com/";

/**
 * Убирает `"https://github.com/"` из полной GitHub-ссылки, оставляя
 * короткий `"owner/repo"` — так и в старом GUI: длинный URL с протоколом и
 * доменом распирал бы узкую боковую панель или строку списка, а
 * `"owner/repo"` тут же понятен без клика.
 *
 * Если строка не начинается с `"https://github.com/"` (например, это уже
 * короткий путь или ссылка на другой хостинг), возвращается без изменений.
 *
 * @param url - полная ссылка (например, `RepoDetail.url`) или уже короткий путь.
 * @returns Ссылка без префикса `"https://github.com/"`.
 *
 * @example
 * githubShortPath("https://github.com/example-org/graph-toolkit"); // "example-org/graph-toolkit"
 * githubShortPath("example-org/graph-toolkit"); // "example-org/graph-toolkit" — префикса и так не было
 */
export function githubShortPath(url: string): string {
  return url.replace(GITHUB_URL_PREFIX, "");
}

/**
 * Строит полную ссылку на GitHub-профиль по имени пользователя —
 * используется вместе с {@link githubShortPath} тем же общим префиксом
 * {@link GITHUB_URL_PREFIX}, так что "собрать ссылку" и "укоротить ссылку"
 * не могут разойтись между собой.
 *
 * @param username - логин пользователя на GitHub (`AuthorDetail.github`), без протокола и домена.
 * @returns Полная ссылка вида `"https://github.com/<username>"`.
 *
 * @example
 * githubProfileUrl("ivanov-ii"); // "https://github.com/ivanov-ii"
 */
export function githubProfileUrl(username: string): string {
  return `${GITHUB_URL_PREFIX}${username}`;
}

/**
 * Возвращает подпись узла для интерфейса на нужном языке.
 *
 * У `PubNode` своего `label` нет вообще — заголовок публикации приходит
 * отдельно, из `PubDetail` (`graph-search.js`), а не из самого узла
 * графа (см. contracts/graph.ts). `pubDetails` — необязательный
 * параметр: если для публикации нашлось название, используется оно; если
 * карта не передана или в ней нет такого ключа, функция откатывается на
 * `node.key` — так вызывающему коду, у которого ещё нет доступа к
 * `PubDetail`, не обязательно ничего менять.
 *
 * У `AuthorNode` есть пара `label`/`label_en` — язык переключается через
 * {@link localize}. У `RepoNode` своего `_en`-варианта нет (имя
 * репозитория не переводится), поэтому для него `localize()` просто
 * вернёт `repo.label` на любом языке. У `PubDetail` тоже нет
 * `_en`-варианта (реальный `graph-search.js` его не содержит) — название
 * публикации всегда на одном языке, независимо от `lang`.
 *
 * @param node - любой из трёх видов узлов графа.
 * @param lang - язык интерфейса.
 * @param pubDetails - опциональная карта деталей публикаций (см.
 *   {@link indexDetailsByKey}) — нужна только для публикаций.
 * @returns Подпись узла на нужном языке (или его ключ, если подписи взять неоткуда).
 *
 * @example
 * nodeLabel(author, "ru"); // "Иванов И.И." (из author.label)
 * nodeLabel(author, "en"); // "Ivanov I.I." (из author.label_en)
 * nodeLabel(repo, "en");   // "graph-toolkit" (у RepoNode нет _en, localize вернёт как есть)
 * nodeLabel(pub, "ru");                        // "P1" — pubDetails не передали, откат на ключ
 * nodeLabel(pub, "ru", pubDetailsByKey);     // "Название публикации" — нашли по ключу
 */
export function nodeLabel(node: GraphNode, lang: Lang, pubDetails?: Map<string, PubDetail>): string {
  if (!("label" in node)) return pubDetails?.get(node.key)?.label ?? node.key;
  const labelEn = "label_en" in node ? node.label_en : undefined;
  return localize(node.label, labelEn, lang);
}
