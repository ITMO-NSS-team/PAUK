import { renderList, renderListItem } from "../../core/render";
import type { AppState } from "../../core/state";
import type { TabModule } from "./types";

/**
 * Вкладка "Репозитории" — список, отсортированный по звёздам по убыванию
 * (как и в старом tab-repos.js). Устройство один в один как у authorsTab —
 * общий механизм выбора через store.selection, подписка на весь store
 * ради подсветки текущего выбора в списке. Подробное объяснение паттерна —
 * в features/tabs/authors.ts.
 */
export const reposTab: TabModule = {
  mount(container, store, map, data) {
    const sortedRepos = [...data.repos].sort((a, b) => b.stars - a.stars);

    function render(state: AppState): void {
      const selectedKey = state.selection?.kind === "node" ? state.selection.key : null;

      renderList(container, sortedRepos, (repo) =>
        renderListItem({
          label: repo.label,
          // ★ — тот же символ, что использовался в старом GUI для звёзд репозитория.
          meta: `★ ${repo.stars}`,
          selected: repo.key === selectedKey,
          onClick: () => {
            store.set({ selection: { kind: "node", key: repo.key } });
            map.flyTo({ center: [repo.gx, repo.gy] });
          },
        }),
      );
    }

    render(store.get());
    return store.subscribe(render);
  },
};
