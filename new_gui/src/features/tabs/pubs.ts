import type { PubNode } from "../../contracts/graph";
import { nodeLabel } from "../../core/data";
import { t } from "../../core/i18n";
import { createNodeListTab } from "./nodeListTab";

/**
 * Вкладка "Публикации" — список, отсортированный по году по убыванию,
 * публикации без года (`year === null`) — в конце списка. Устройство —
 * общее для всех вкладок-списков (см. {@link createNodeListTab}), с одной
 * особенностью: у `PubNode` нет своего `label` — настоящее название
 * приходит из `pubDetails` (`pubs-detail.json`, см. `core/data.ts::loadDetails()`),
 * {@link nodeLabel} откатится на ключ публикации, только если для неё нет
 * записи в `pubDetails`.
 *
 * Реализует {@link TabModule}.
 */
export const pubsTab = createNodeListTab<PubNode>({
  items: (data) => data.pubs,
  compare: (a, b) => (b.year ?? -Infinity) - (a.year ?? -Infinity),
  label: (pub, state, pubDetails) => nodeLabel(pub, state.lang, pubDetails),
  meta: (pub, state) => (pub.year === null ? t("field.yearUnknown", state.lang) : String(pub.year)),
});
