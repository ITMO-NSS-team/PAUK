import type { SearchHit } from "../../contracts/search";
import { indexByKey } from "../../core/data";
import { kindLabel, t, type Lang } from "../../core/i18n";
import { renderList, renderListItem } from "../../core/render";
import type { TabId } from "../../core/state";
import { buildSearchIndex, parseDeptHitKey, searchHits } from "../search";
import type { TabModule } from "./types";

/**
 * Какая вкладка отвечает за узел найденного вида — как и в старом
 * search.js::runSearch() (targetTab = repo ? 2 : pub ? 3 : 1). Департамента
 * здесь нет: у него нет своей вкладки с графом, dept-хиты вкладку не меняют.
 */
const HIT_KIND_TAB: Record<"author" | "repo" | "pub", TabId> = { author: 1, repo: 2, pub: 3 };

/**
 * Вкладка "Поиск" — полноэкранный (в рамках сайдбара) поиск по всем
 * авторам, репозиториям, публикациям и департаментам сразу, в отличие от
 * вкладок 1-3, которые показывают только один вид узлов. Устройство
 * похоже на остальные вкладки (renderList + store.selection), но список
 * строится не из data напрямую, а из текстового запроса пользователя —
 * поэтому здесь есть собственное локальное состояние (query), которое
 * не кладём в общий Store: это состояние поля ввода одной конкретной
 * вкладки, а не что-то, на что должны реагировать другие части приложения.
 */
export const searchTab: TabModule = {
  mount(container, store, map, data, searchDetails) {
    const nodeByKey = indexByKey(data);

    let query = "";
    // Индекс поиска зависит от языка (label/sub переведены) — пересобирается
    // заново только когда lang реально поменялся, не на каждое изменение store.
    let index: SearchHit[] = buildSearchIndex(data, store.get().lang, searchDetails);

    const input = document.createElement("input");
    input.type = "search";
    input.className = "search-input";

    const results = document.createElement("div");
    results.className = "search-results";

    function renderResults(lang: Lang): void {
      const hits = searchHits(index, query);
      renderList(results, hits, (hit) =>
        renderListItem({
          label: `${kindLabel(hit.kind, lang)}: ${hit.label}`,
          meta: hit.sub ?? undefined,
          // Пригодится и для стилизации по виду результата, и чтобы найти
          // конкретный результат в тестах, не завязываясь на порядок в списке.
          dataKind: hit.kind,
          onClick: () => {
            if (hit.kind === "dept") {
              store.set({ selection: { kind: "dept", id: parseDeptHitKey(hit.key) } });
              return;
            }

            // Переключаем вкладку на ту, что реально рисует граф для этого
            // вида узла — иначе выбор был бы невидим: карта показывает граф
            // только активной вкладки (map/build.ts), а поиск сам живёт на
            // отдельной вкладке 4 без собственного графа. Одним store.set(),
            // а не двумя — иначе подписчики (mountReactiveGraph и др.)
            // перерисовались бы дважды на один клик.
            store.set({ tab: HIT_KIND_TAB[hit.kind], selection: { kind: "node", key: hit.key } });
            const node = nodeByKey.get(hit.key);
            if (node) map.flyTo({ center: [node.gx, node.gy] });
          },
        }),
      );
    }

    function applyLang(lang: Lang): void {
      input.placeholder = t("search.placeholder", lang);
      renderResults(lang);
    }

    input.addEventListener("input", () => {
      query = input.value;
      renderResults(store.get().lang);
    });

    container.replaceChildren(input, results);
    applyLang(store.get().lang);

    let currentLang = store.get().lang;
    const unsubscribe = store.subscribe((state) => {
      if (state.lang === currentLang) return;
      currentLang = state.lang;
      index = buildSearchIndex(data, currentLang, searchDetails);
      applyLang(currentLang);
    });

    return unsubscribe;
  },
};
