import type { AuthorDetail, GraphData, PubDetail, RepoDetail } from "../../contracts/graph";
import type { SearchHit } from "../../contracts/search";
import { githubShortPath, nodeLabel } from "../../core/data";
import { localize, t, type Lang } from "../../core/i18n";

const DEPT_KEY_PREFIX = "dept:";

/** Departments have only a numeric id, while SearchHit.key is a string. */
export function deptHitKey(deptId: number): string {
  return `${DEPT_KEY_PREFIX}${deptId}`;
}

export function parseDeptHitKey(key: string): number {
  return Number(key.slice(DEPT_KEY_PREFIX.length));
}

/**
 * @param authorDetails - Full names and spellings go to `terms`, so an author
 *   is found in either alphabet. Empty map: search by labels only.
 */
export function buildSearchIndex(
  data: GraphData,
  lang: Lang,
  pubDetails: Map<string, PubDetail>,
  repoDetails: Map<string, RepoDetail>,
  authorDetails = new Map<string, AuthorDetail>(),
): SearchHit[] {
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
    terms: spellings([
      author.label,
      author.label_en,
      authorDetails.get(author.key)?.name_ru,
      authorDetails.get(author.key)?.name_en,
      ...(authorDetails.get(author.key)?.name_variants.openalex ?? []),
      ...(authorDetails.get(author.key)?.name_variants.orcid ?? []),
    ]),
  }));

  const repoHits: SearchHit[] = data.repos.map((repo) => {
    const url = repoDetails.get(repo.key)?.url;
    return {
      key: repo.key,
      kind: "repo",
      label: repo.label,
      sub: url ? githubShortPath(url) : null,
    };
  });

  const pubHits: SearchHit[] = data.pubs.map((pub) => ({
    key: pub.key,
    kind: "pub",
    label: nodeLabel(pub, lang, pubDetails),
    sub:
      [pub.year, deptName(pub.dept), pubDetails.get(pub.key)?.journal]
        .filter(Boolean)
        .join(" · ") || null,
  }));

  const deptHits: SearchHit[] = data.departments.map((dept) => ({
    key: deptHitKey(dept.id),
    kind: "dept",
    label: localize(dept.name, dept.name_en, lang),
    sub: null,
    terms: spellings([dept.name, dept.name_en, ...(dept.name_variants ?? [])]),
  }));

  return [...authorHits, ...repoHits, ...pubHits, ...deptHits];
}

/** An empty query returns nothing, not everything. */
export function searchHits(index: SearchHit[], query: string): SearchHit[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return [];

  return index.filter(
    (hit) =>
      hit.label.toLowerCase().includes(needle) ||
      (hit.sub?.toLowerCase().includes(needle) ?? false) ||
      (hit.terms?.includes(needle) ?? false),
  );
}

function spellings(names: (string | undefined)[]): string {
  return names
    .filter((name): name is string => Boolean(name))
    .join("\n")
    .toLowerCase();
}
