import { localize } from "../../core/i18n";
import { renderList } from "../../core/render";
import type { AppState } from "../../core/state";
import type { TabModule } from "./types";

/**
 * Вкладка "Авторы" — список всех авторов, отсортированный по количеству
 * публикаций по убыванию (как и в старом tab-authors.js). Клик по автору
 * в списке пишет тот же store.selection, что и клик по узлу на карте —
 * это один и тот же механизм выбора с двумя входами (карта и список),
 * поэтому подсветка на карте (map/build.ts::setSelectedNode, через
 * features/selection.ts) срабатывает одинаково независимо от источника
 * клика. И наоборот: если узел выбрали кликом по карте, в этом списке
 * подсвечивается соответствующая строка — для этого render() подписан
 * на весь store и на каждое изменение перечитывает state.selection.
 */
export const authorsTab: TabModule = {
  mount(container, store, map, data) {
    // Сортировка один раз при монтировании — сами данные вкладки не
    // меняются, меняется только то, что в ней выбрано.
    const sortedAuthors = [...data.authors].sort((a, b) => b.pubs_count - a.pubs_count);

    function render(state: AppState): void {
      // Один раз достаём ключ выбранного узла (если выбран именно узел,
      // а не ребро/департамент) — дальше сравниваем с ним каждую строку.
      const selectedKey = state.selection?.kind === "node" ? state.selection.key : null;

      renderList(container, sortedAuthors, (author) => {
        const item = document.createElement("button");
        // type="button" обязателен — иначе клик по кнопке внутри произвольной
        // формы попытался бы её отправить (сабмит); тут формы нет, но
        // задавать type явно — привычка, которая на будущее не даст об это споткнуться.
        item.type = "button";
        item.className = "tab-list-item";
        if (author.key === selectedKey) item.classList.add("tab-list-item--selected");

        const name = document.createElement("span");
        name.className = "tab-list-item__label";
        name.textContent = localize(author.label, author.label_en, state.lang);

        const count = document.createElement("span");
        count.className = "tab-list-item__meta";
        count.textContent = String(author.pubs_count);

        item.append(name, count);
        item.addEventListener("click", () => {
          store.set({ selection: { kind: "node", key: author.key } });
          // Подлетаем к автору на карте, не меняя zoom — просто центрируем,
          // чтобы выбранная точка не осталась за пределами экрана.
          map.flyTo({ center: [author.gx, author.gy] });
        });

        return item;
      });
    }

    render(store.get());
    return store.subscribe(render);
  },
};
