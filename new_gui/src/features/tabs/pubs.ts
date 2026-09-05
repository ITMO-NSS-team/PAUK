import { nodeLabel } from "../../core/data";
import { t } from "../../core/i18n";
import { renderList, renderListItem } from "../../core/render";
import type { AppState } from "../../core/state";
import type { TabModule } from "./types";

/**
 * Вкладка "Публикации" — список, отсортированный по году по убыванию,
 * публикации без года (`year === null`) — в конце списка. Устройство то
 * же, что у `authorsTab`/`reposTab` (см. `features/tabs/authors.ts` за
 * подробным объяснением), с одной особенностью: у `PubNode` нет своего
 * `label` — настоящее название приходит из `searchDetails`
 * (`core/data.ts::loadSampleSearchDetails()`, синтетический аналог
 * `graph-search.js`), {@link nodeLabel} откатится на ключ публикации,
 * только если для неё нет записи в `searchDetails`.
 *
 * Реализует {@link TabModule}.
 */
export const pubsTab: TabModule = {
  mount(container, store, map, data, searchDetails) {
    const sortedPubs = [...data.pubs].sort((a, b) => (b.year ?? -Infinity) - (a.year ?? -Infinity));

    /**
     * Перерисовывает список публикаций под текущий язык (влияет на
     * `nodeLabel`) и подсветку выбора.
     *
     * @param state - текущее состояние приложения.
     */
    function render(state: AppState): void {
      const selectedKey = state.selection?.kind === "node" ? state.selection.key : null;

      renderList(container, sortedPubs, (pub) =>
        renderListItem({
          label: nodeLabel(pub, state.lang, searchDetails),
          meta: pub.year === null ? t("field.yearUnknown", state.lang) : String(pub.year),
          selected: pub.key === selectedKey,
          onClick: () => {
            store.set({ selection: { kind: "node", key: pub.key } });
            map.flyTo({ center: [pub.gx, pub.gy] });
          },
        }),
      );
    }

    render(store.get());
    return store.subscribe(render);
  },
};
