import type { AuthorNode } from "../../contracts/graph";
import { localize } from "../../core/i18n";
import { createNodeListTab } from "./nodeListTab";

/**
 * Вкладка "Авторы" — список всех авторов, отсортированный по количеству
 * публикаций по убыванию (как и в старом `tab-authors.js`). Общий механизм
 * выбора (клик по строке списка = клик по узлу карты, и наоборот) и
 * подписка на Store — в {@link createNodeListTab}, здесь только то, чем
 * эта вкладка отличается от reposTab/pubsTab: сортировка и что показать в строке.
 *
 * Реализует {@link TabModule} — см. её JSDoc за подробным описанием формы `mount()`.
 */
export const authorsTab = createNodeListTab<AuthorNode>({
  items: (data) => data.authors,
  compare: (a, b) => b.pubs_count - a.pubs_count,
  label: (author, state) => localize(author.label, author.label_en, state.lang),
  meta: (author) => String(author.pubs_count),
});
