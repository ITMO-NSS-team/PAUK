import type { PubNode } from "../../contracts/graph";
import { nodeLabel } from "../../core/data";
import { t } from "../../core/i18n";
import { createNodeListTab } from "./nodeListTab";

/** Newest first, publications without a year last. */
export const pubsTab = createNodeListTab<PubNode>({
  items: (data) => data.pubs,
  compare: (a, b) => (b.year ?? -Infinity) - (a.year ?? -Infinity),
  label: (pub, state, pubDetails) => nodeLabel(pub, state.lang, pubDetails),
  meta: (pub, state) => (pub.year === null ? t("field.yearUnknown", state.lang) : String(pub.year)),
});
