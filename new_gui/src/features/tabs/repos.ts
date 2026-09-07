import type { RepoNode } from "../../contracts/graph";
import { createNodeListTab } from "./nodeListTab";

/**
 * Вкладка "Репозитории" — список, отсортированный по звёздам по убыванию
 * (как и в старом `tab-repos.js`). Устройство один в один как у
 * `authorsTab` (features/tabs/authors.ts) — общий механизм в
 * {@link createNodeListTab}, подробное объяснение паттерна — там же.
 *
 * Реализует {@link TabModule}.
 */
export const reposTab = createNodeListTab<RepoNode>({
  items: (data) => data.repos,
  compare: (a, b) => b.stars - a.stars,
  label: (repo) => repo.label,
  // ★ — тот же символ, что использовался в старом GUI для звёзд репозитория.
  meta: (repo) => `★ ${repo.stars}`,
});
