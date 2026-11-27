import type { PubNode } from "../../contracts/graph";
import { nodeLabel } from "../../core/data";
import { t } from "../../core/i18n";
import { isNodeShown } from "../../map/build";
import { createNodeListTab } from "./nodeListTab";

export const pubsTab = createNodeListTab<PubNode>({
  items: (data) => data.pubs,
  visible: (pub, state, data) => isNodeShown(data, pub, state.filters),
  compare: (a, b) => (b.year ?? -Infinity) - (a.year ?? -Infinity),
  label: (pub, state, pubDetails) => nodeLabel(pub, state.lang, pubDetails),
  meta: (pub, state) => (pub.year === null ? t("field.yearUnknown", state.lang) : String(pub.year)),
});
