// Слой "features" — построение и фильтрация индекса поиска. Формат
// SearchHit (contracts/search.ts) не то, что пишет Python-генератор —
// индекс строится здесь же, в браузере, из уже загруженного GraphData
// (ровно как и в старом search.js).

import type { GraphData } from "../../contracts/graph";
import type { SearchDetail, SearchHit } from "../../contracts/search";
import { githubShortPath, nodeLabel } from "../../core/data";
import { localize, t, type Lang } from "../../core/i18n";

// Формат ключа department-хита ("dept:<id>") — в одном месте, чтобы
// сборка (deptHitKey) и разбор (parseDeptHitKey) точно не разъехались.
const DEPT_KEY_PREFIX = "dept:";

/**
 * Строит ключ результата поиска для департамента — департаменты, в отличие
 * от узлов графа, не имеют своего строкового `key` (только числовой `id`),
 * а `SearchHit.key` должен быть строкой, единой для всех видов результатов.
 *
 * @param deptId - числовой id департамента (`Department.id`).
 * @returns Строковый ключ вида `"dept:<id>"`.
 *
 * @example
 * deptHitKey(0); // "dept:0"
 */
export function deptHitKey(deptId: number): string {
  return `${DEPT_KEY_PREFIX}${deptId}`;
}

/**
 * Обратное преобразование к {@link deptHitKey}: достаёт числовой id
 * департамента из ключа результата поиска.
 *
 * @param key - ключ результата поиска вида `"dept:<id>"` (см. {@link deptHitKey}).
 * @returns Числовой id департамента.
 *
 * @example
 * parseDeptHitKey("dept:0"); // 0
 * parseDeptHitKey(deptHitKey(42)) === 42; // true — обратимость гарантирована тестом
 */
export function parseDeptHitKey(key: string): number {
  return Number(key.slice(DEPT_KEY_PREFIX.length));
}

/**
 * Строит плоский индекс результатов поиска из всех авторов, репозиториев,
 * публикаций и департаментов сразу. Вызывается один раз при монтировании
 * вкладки поиска и при каждой смене языка (см. features/tabs/search.ts) —
 * не на каждое нажатие клавиши, это отдельная функция {@link searchHits}.
 *
 * `sub` — короткая вторая строка под основным названием, как и в старом
 * GUI: для автора это департамент и число публикаций, для репозитория —
 * путь на GitHub без `"https://"`, для публикации — год, департамент и
 * (если есть) журнал, у департамента своей `sub` нет.
 *
 * @param data - данные графа.
 * @param lang - язык интерфейса (влияет на `label`/`sub` каждого результата).
 * @param searchDetails - карта деталей публикаций (см. `core/data.ts::indexSearchDetailsByKey`) —
 *   нужна, чтобы у публикаций в поиске было настоящее название и журнал, а не голый ключ.
 * @returns Список результатов поиска всех видов, в порядке author → repo → pub → dept.
 *
 * @example
 * const index = buildSearchIndex(data, "ru", searchDetailsByKey);
 * index.find((hit) => hit.kind === "dept");
 * // { key: "dept:0", kind: "dept", label: "Институт прикладных систем", sub: null }
 */
export function buildSearchIndex(data: GraphData, lang: Lang, searchDetails: Map<string, SearchDetail>): SearchHit[] {
  const deptById = new Map(data.departments.map((dept) => [dept.id, dept]));
  const deptName = (id: number): string => {
    const dept = deptById.get(id);
    return dept ? localize(dept.name, dept.name_en, lang) : t("field.unknownDept", lang);
  };

  const authorHits: SearchHit[] = data.authors.map((author) => ({
    key: author.key,
    kind: "author",
    label: localize(author.label, author.label_en, lang),
    sub: `${deptName(author.dept)} · ${author.pubs_count} ${t("search.pubsCountShort", lang)}`,
  }));

  const repoHits: SearchHit[] = data.repos.map((repo) => ({
    key: repo.key,
    kind: "repo",
    label: repo.label,
    // Ссылка на GitHub обычно длиннее видимого места в списке — оставляем
    // только "owner/repo", без протокола и домена.
    sub: githubShortPath(repo.url),
  }));

  const pubHits: SearchHit[] = data.pubs.map((pub) => ({
    key: pub.key,
    kind: "pub",
    label: nodeLabel(pub, lang, searchDetails),
    // Журнал добавляется, только если для публикации нашлась запись в
    // searchDetails — без неё (как и раньше) остаются год и департамент.
    sub: [pub.year, deptName(pub.dept), searchDetails.get(pub.key)?.journal].filter(Boolean).join(" · ") || null,
  }));

  const deptHits: SearchHit[] = data.departments.map((dept) => ({
    key: deptHitKey(dept.id),
    kind: "dept",
    label: localize(dept.name, dept.name_en, lang),
    sub: null,
  }));

  return [...authorHits, ...repoHits, ...pubHits, ...deptHits];
}

/**
 * Фильтрует индекс результатов поиска по подстроке в названии или в `sub`,
 * без учёта регистра. Пустой запрос даёт пустой список результатов (а не
 * "показать всё") — так же вело себя старое полноэкранное окно поиска.
 *
 * @param index - индекс результатов (см. {@link buildSearchIndex}).
 * @param query - текст запроса, как он есть в поле ввода (пробелы по краям обрезаются).
 * @returns Отфильтрованный список результатов, в том же порядке, что и в `index`.
 *
 * @example
 * const hits = searchHits(index, "иванов");
 * // все результаты, чьи label/sub содержат "иванов" без учёта регистра
 *
 * searchHits(index, "");   // [] — пустой запрос не значит "показать всё"
 * searchHits(index, "  "); // [] — то же самое для запроса из одних пробелов
 */
export function searchHits(index: SearchHit[], query: string): SearchHit[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return [];

  return index.filter(
    (hit) => hit.label.toLowerCase().includes(needle) || (hit.sub?.toLowerCase().includes(needle) ?? false),
  );
}
