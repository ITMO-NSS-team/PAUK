// Слой "features" — построение и фильтрация индекса поиска. Формат
// SearchHit (contracts/search.ts) не то, что пишет Python-генератор —
// индекс строится здесь же, в браузере, из уже загруженного GraphData
// (ровно как и в старом search.js).

import type { GraphData } from "../../contracts/graph";
import type { SearchDetail, SearchHit } from "../../contracts/search";
import { nodeLabel } from "../../core/data";
import { localize, t, type Lang } from "../../core/i18n";

// Формат ключа department-хита ("dept:<id>") — в одном месте, чтобы
// сборка (deptHitKey) и разбор (parseDeptHitKey) точно не разъехались.
const DEPT_KEY_PREFIX = "dept:";

/** department id -> ключ SearchHit. */
export function deptHitKey(deptId: number): string {
  return `${DEPT_KEY_PREFIX}${deptId}`;
}

/** Обратное преобразование: ключ department-хита -> department id. */
export function parseDeptHitKey(key: string): number {
  return Number(key.slice(DEPT_KEY_PREFIX.length));
}

/**
 * Строит плоский индекс из всех авторов, репозиториев, публикаций и
 * департаментов — вызывается один раз при монтировании вкладки поиска,
 * а не на каждое нажатие клавиши. sub — короткая вторая строка под
 * основным названием, как и в старом GUI: для автора это департамент и
 * число публикаций, для репозитория — путь на GitHub без "https://",
 * для публикации — год и департамент, у департамента своей sub нет.
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
    sub: repo.url.replace("https://github.com/", ""),
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
 * Фильтрует индекс по подстроке в названии или в sub, без учёта регистра.
 * Пустой запрос — пустой список результатов (а не "показать всё") —
 * так же вело себя старое полноэкранное окно поиска.
 */
export function searchHits(index: SearchHit[], query: string): SearchHit[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return [];

  return index.filter(
    (hit) => hit.label.toLowerCase().includes(needle) || (hit.sub?.toLowerCase().includes(needle) ?? false),
  );
}
