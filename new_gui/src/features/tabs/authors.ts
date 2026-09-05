import { localize } from "../../core/i18n";
import { renderList, renderListItem } from "../../core/render";
import type { AppState } from "../../core/state";
import type { TabModule } from "./types";

/**
 * Вкладка "Авторы" — список всех авторов, отсортированный по количеству
 * публикаций по убыванию (как и в старом `tab-authors.js`). Клик по автору
 * в списке пишет тот же `store.selection`, что и клик по узлу на карте —
 * это один и тот же механизм выбора с двумя входами (карта и список),
 * поэтому подсветка на карте (`map/build.ts::setSelectedNode`, через
 * `features/selection.ts`) срабатывает одинаково независимо от источника
 * клика. И наоборот: если узел выбрали кликом по карте, в этом списке
 * подсвечивается соответствующая строка — для этого `render()` подписан
 * на весь store и на каждое изменение перечитывает `state.selection`.
 *
 * Реализует {@link TabModule} — см. её JSDoc за подробным описанием формы `mount()`.
 */
export const authorsTab: TabModule = {
  mount(container, store, map, data) {
    // Сортировка один раз при монтировании — сами данные вкладки не
    // меняются, меняется только то, что в ней выбрано.
    const sortedAuthors = [...data.authors].sort((a, b) => b.pubs_count - a.pubs_count);

    /**
     * Перерисовывает список авторов под текущий язык и подсветку выбора.
     * Вызывается при монтировании и на каждое изменение Store.
     *
     * @param state - текущее состояние приложения.
     */
    function render(state: AppState): void {
      // Один раз достаём ключ выбранного узла (если выбран именно узел,
      // а не ребро/департамент) — дальше сравниваем с ним каждую строку.
      const selectedKey = state.selection?.kind === "node" ? state.selection.key : null;

      renderList(container, sortedAuthors, (author) =>
        renderListItem({
          label: localize(author.label, author.label_en, state.lang),
          meta: String(author.pubs_count),
          selected: author.key === selectedKey,
          onClick: () => {
            store.set({ selection: { kind: "node", key: author.key } });
            // Подлетаем к автору на карте, не меняя zoom — просто
            // центрируем, чтобы выбранная точка не осталась за экраном.
            map.flyTo({ center: [author.gx, author.gy] });
          },
        }),
      );
    }

    render(store.get());
    return store.subscribe(render);
  },
};
