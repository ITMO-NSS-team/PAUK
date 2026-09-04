import { renderList } from "../../core/render";
import type { AppState } from "../../core/state";
import type { TabModule } from "./types";

/**
 * Вкладка "Репозитории" — список, отсортированный по звёздам по убыванию
 * (как и в старом tab-repos.js). Устройство один в один как у authorsTab —
 * общий механизм выбора через store.selection, подписка на весь store
 * ради подсветки текущего выбора в списке. Комментарии здесь короче,
 * подробное объяснение паттерна — в features/tabs/authors.ts.
 */
export const reposTab: TabModule = {
  mount(container, store, map, data) {
    const sortedRepos = [...data.repos].sort((a, b) => b.stars - a.stars);

    function render(state: AppState): void {
      const selectedKey = state.selection?.kind === "node" ? state.selection.key : null;

      renderList(container, sortedRepos, (repo) => {
        const item = document.createElement("button");
        item.type = "button";
        item.className = "tab-list-item";
        if (repo.key === selectedKey) item.classList.add("tab-list-item--selected");

        const name = document.createElement("span");
        name.className = "tab-list-item__label";
        name.textContent = repo.label;

        const stars = document.createElement("span");
        stars.className = "tab-list-item__meta";
        // ★ — тот же символ, что использовался в старом GUI для звёзд репозитория.
        stars.textContent = `★ ${repo.stars}`;

        item.append(name, stars);
        item.addEventListener("click", () => {
          store.set({ selection: { kind: "node", key: repo.key } });
          map.flyTo({ center: [repo.gx, repo.gy] });
        });

        return item;
      });
    }

    render(store.get());
    return store.subscribe(render);
  },
};
