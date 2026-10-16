import type { AuthorNode } from "../../contracts/graph";
import { localize } from "../../core/i18n";
import { isAuthorShown } from "../../map/build";
import { createNodeListTab } from "./nodeListTab";

export const authorsTab = createNodeListTab<AuthorNode>({
  items: (data) => data.authors,
  visible: (author, state, data) => isAuthorShown(data, author, state.filters),
  compare: (a, b) => b.pubs_count - a.pubs_count,
  label: (author, state) => localize(author.label, author.label_en, state.lang),
  meta: (author) => String(author.pubs_count),
});
