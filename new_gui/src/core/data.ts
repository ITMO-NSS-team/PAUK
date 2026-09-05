import type { AuthorNode, GraphData, PubNode, RepoAuthorEdge, RepoNode } from "../contracts/graph";
import type { SearchDetail } from "../contracts/search";
import { localize, type Lang } from "./i18n";
import sampleGraphData from "./fixtures/graph-data.sample.json";
import sampleSearchDetails from "./fixtures/graph-search.sample.json";

/** Любой из трёх видов узлов графа — авторы, репозитории, публикации. */
type GraphNode = AuthorNode | RepoNode | PubNode;

/**
 * Разбирает "обёрнутый" JSON-файл легаси-формата вида `window.GRAPH={...};`
 * (так генератор `pauk/gui/generate_data.py` пишет данные для подключения
 * через `<script>`, а не как обычный `.json`). Отрезает известные строки
 * `prefix`/`suffix` по краям и парсит то, что осталось, как обычный JSON.
 *
 * Старый и новый GUI работают с одними и теми же файлами, которые
 * генерирует Python — трогать генератор нам сейчас нельзя, поэтому new_gui
 * сам разбирает существующий формат на клиенте.
 *
 * Вынесена в отдельную чистую функцию (без сети, без побочных эффектов),
 * чтобы её можно было проверить тестом прямо на содержимом реального файла,
 * не поднимая fetch/сервер.
 *
 * @typeParam T - ожидаемая форма распарсенных данных (в приложении — {@link GraphData} или `SearchDetail[]`).
 * @param text - полное содержимое файла как строка.
 * @param prefix - строка в самом начале файла, которую нужно отрезать (например, `"window.GRAPH="`).
 * @param suffix - строка в самом конце файла, которую нужно отрезать (например, `";"`, либо `""`, если её нет).
 * @returns Распарсенный JSON, приведённый к типу `T`.
 * @throws Error, если текст не начинается с `prefix` или не заканчивается на `suffix`.
 *
 * @example
 * parseWrappedJson<{ a: number }>('window.X={"a":1};', "window.X=", ";");
 * // { a: 1 }
 */
export function parseWrappedJson<T>(text: string, prefix: string, suffix: string): T {
  if (!text.startsWith(prefix) || !text.endsWith(suffix)) {
    throw new Error(`неожиданный формат файла данных: не начинается с "${prefix}" или не кончается на "${suffix}"`);
  }

  const json = suffix.length > 0 ? text.slice(prefix.length, -suffix.length) : text.slice(prefix.length);
  return JSON.parse(json) as T;
}

/**
 * Загружает настоящий `graph-data.js` по сети (легаси-формат
 * `window.GRAPH=...;`, см. {@link parseWrappedJson}) и проверяет его форму
 * в dev-режиме через {@link assertGraphData}.
 *
 * Пока не используется нигде в приложении: v2-прототип временно работает
 * на синтетическом фикстур-наборе (см. {@link loadSampleGraphData} ниже) —
 * реальные данные подключим отдельным шагом, когда дойдём до интеграции с
 * генератором. Функция уже написана и протестирована (`parseWrappedJson`
 * покрыт тестом на реальном формате файла) заранее, чтобы этот следующий
 * шаг был не "написать загрузку", а просто "начать её вызывать".
 *
 * @param url - адрес файла `graph-data.js` (например, из Vite dev-сервера прокси или статики).
 * @returns Промис с данными графа.
 * @throws Error, если HTTP-запрос не удался (`!response.ok`) или содержимое
 *   не в ожидаемом формате `window.GRAPH=...;` (см. {@link parseWrappedJson}).
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
 * Загружает синтетический аналог `graph-search.js` — детали публикаций
 * (настоящее название, журнал, DOI, ссылка на код), которых нет в самом
 * `GraphData`. Соответствует по ключам публикациям из
 * {@link loadSampleGraphData} (P1-P6) — реальный `graph-search.js`
 * подключим тем же следующим шагом, что и `graph-data.js` (см.
 * {@link loadGraphData}).
 *
 * @returns Промис со списком деталей публикаций.
 */
export async function loadSampleSearchDetails(): Promise<SearchDetail[]> {
  return sampleSearchDetails as SearchDetail[];
}

/**
 * Строит индекс "ключ публикации -> её SearchDetail" для мгновенного
 * поиска по ключу — тот же принцип, что и {@link indexByKey} ниже, только
 * для деталей публикаций, а не узлов графа.
 *
 * @param details - список деталей публикаций (например, результат {@link loadSampleSearchDetails}).
 * @returns Map от `SearchDetail.key` к самому объекту `SearchDetail`.
 *
 * @example
 * const byKey = indexSearchDetailsByKey([{ key: "P1", label: "...", ... }]);
 * byKey.get("P1"); // { key: "P1", label: "...", ... }
 * byKey.get("P2"); // undefined — такого ключа не было в списке
 */
export function indexSearchDetailsByKey(details: SearchDetail[]): Map<string, SearchDetail> {
  return new Map(details.map((detail) => [detail.key, detail]));
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
 * работал", а не только владелец из `RepoNode.owner`.
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
 * Строит индекс "репозиторий -> ключи связанных публикаций" из
 * `GraphData.repo_pub_edges`.
 *
 * Нужен карточке репозитория (features/panels.ts): "какие публикации с ним
 * связаны".
 *
 * @param data - данные графа.
 * @returns Map от ключа репозитория к списку ключей публикаций.
 *
 * @example
 * // repo_pub_edges: [{s:"R1",t:"P1"}]
 * buildRepoPubIndex(data).get("R1"); // ["P1"]
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
 * @param url - полная ссылка (например, `RepoNode.url`) или уже короткий путь.
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
 * @param username - логин пользователя на GitHub (`AuthorNode.github`), без протокола и домена.
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
 * отдельно, из `SearchDetail` (`graph-search.js`), а не из самого узла
 * графа (см. contracts/graph.ts). `searchDetails` — необязательный
 * параметр: если для публикации нашлось название, используется оно; если
 * карта не передана или в ней нет такого ключа, функция откатывается на
 * `node.key` — так вызывающему коду, у которого ещё нет доступа к
 * `SearchDetail`, не обязательно ничего менять.
 *
 * У `AuthorNode` есть пара `label`/`label_en` — язык переключается через
 * {@link localize}. У `RepoNode` своего `_en`-варианта нет (имя
 * репозитория не переводится), поэтому для него `localize()` просто
 * вернёт `repo.label` на любом языке. У `SearchDetail` тоже нет
 * `_en`-варианта (реальный `graph-search.js` его не содержит) — название
 * публикации всегда на одном языке, независимо от `lang`.
 *
 * @param node - любой из трёх видов узлов графа.
 * @param lang - язык интерфейса.
 * @param searchDetails - опциональная карта деталей публикаций (см.
 *   {@link indexSearchDetailsByKey}) — нужна только для публикаций.
 * @returns Подпись узла на нужном языке (или его ключ, если подписи взять неоткуда).
 *
 * @example
 * nodeLabel(author, "ru"); // "Иванов И.И." (из author.label)
 * nodeLabel(author, "en"); // "Ivanov I.I." (из author.label_en)
 * nodeLabel(repo, "en");   // "graph-toolkit" (у RepoNode нет _en, localize вернёт как есть)
 * nodeLabel(pub, "ru");                        // "P1" — searchDetails не передали, откат на ключ
 * nodeLabel(pub, "ru", searchDetailsByKey);     // "Название публикации" — нашли по ключу
 */
export function nodeLabel(node: GraphNode, lang: Lang, searchDetails?: Map<string, SearchDetail>): string {
  if (!("label" in node)) return searchDetails?.get(node.key)?.label ?? node.key;
  const labelEn = "label_en" in node ? node.label_en : undefined;
  return localize(node.label, labelEn, lang);
}
