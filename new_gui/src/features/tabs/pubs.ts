import { nodeLabel } from "../../core/data";
import { renderList } from "../../core/render";
import type { AppState } from "../../core/state";
import type { TabModule } from "./types";

/**
 * Вкладка "Публикации" — список, отсортированный по году по убыванию,
 * публикации без года (year === null) — в конце списка. Устройство то же,
 * что у authorsTab/reposTab (см. authors.ts для подробного объяснения),
 * с одной особенностью: у PubNode нет своего label, поэтому подпись
 * берём через core/data.ts::nodeLabel() — она возвращает ключ публикации
 * как временную заглушку, пока не подключён SearchDetail с настоящими
 * заголовками (graph-search.js).
 */
export const pubsTab: TabModule = {
  mount(container, store, map, data) {
    const sortedPubs = [...data.pubs].sort((a, b) => (b.year ?? -Infinity) - (a.year ?? -Infinity));

    function render(state: AppState): void {
      const selectedKey = state.selection?.kind === "node" ? state.selection.key : null;

      renderList(container, sortedPubs, (pub) => {
        const item = document.createElement("button");
        item.type = "button";
        item.className = "tab-list-item";
        if (pub.key === selectedKey) item.classList.add("tab-list-item--selected");

        const title = document.createElement("span");
        title.className = "tab-list-item__label";
        title.textContent = nodeLabel(pub);

        const year = document.createElement("span");
        year.className = "tab-list-item__meta";
        year.textContent = pub.year === null ? "год неизвестен" : String(pub.year);

        item.append(title, year);
        item.addEventListener("click", () => {
          store.set({ selection: { kind: "node", key: pub.key } });
          map.flyTo({ center: [pub.gx, pub.gy] });
        });

        return item;
      });
    }

    render(store.get());
    return store.subscribe(render);
  },
};
