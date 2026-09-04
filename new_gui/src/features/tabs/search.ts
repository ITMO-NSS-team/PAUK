import type { SearchHit } from "../../contracts/search";
import { indexByKey } from "../../core/data";
import { renderList } from "../../core/render";
import { buildSearchIndex, parseDeptHitKey, searchHits } from "../search";
import type { TabModule } from "./types";

/** Короткая русская подпись типа результата — показывается перед названием, чтобы не путать одноимённые сущности разных видов. */
const KIND_PREFIX: Record<SearchHit["kind"], string> = {
  author: "Автор",
  repo: "Репозиторий",
  pub: "Публикация",
  dept: "Департамент",
};

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
  mount(container, store, map, data) {
    // Строится один раз при монтировании — сам список авторов/репозиториев/
    // публикаций/департаментов между нажатиями клавиш не меняется.
    const index = buildSearchIndex(data);
    const nodeByKey = indexByKey(data);

    let query = "";

    const input = document.createElement("input");
    input.type = "search";
    input.placeholder = "Поиск по авторам, репозиториям, публикациям, департаментам…";
    input.className = "search-input";

    const results = document.createElement("div");
    results.className = "search-results";

    function renderResults(): void {
      const hits = searchHits(index, query);
      renderList(results, hits, (hit) => {
        const item = document.createElement("button");
        item.type = "button";
        item.className = "tab-list-item";
        // Пригодится и для стилизации по виду результата, и чтобы найти
        // конкретный результат в тестах, не завязываясь на порядок в списке.
        item.dataset.kind = hit.kind;

        const label = document.createElement("span");
        label.className = "tab-list-item__label";
        label.textContent = `${KIND_PREFIX[hit.kind]}: ${hit.label}`;

        item.appendChild(label);
        if (hit.sub) {
          const sub = document.createElement("span");
          sub.className = "tab-list-item__meta";
          sub.textContent = hit.sub;
          item.appendChild(sub);
        }

        item.addEventListener("click", () => {
          if (hit.kind === "dept") {
            store.set({ selection: { kind: "dept", id: parseDeptHitKey(hit.key) } });
            return;
          }

          store.set({ selection: { kind: "node", key: hit.key } });
          const node = nodeByKey.get(hit.key);
          if (node) map.flyTo({ center: [node.gx, node.gy] });
        });

        return item;
      });
    }

    input.addEventListener("input", () => {
      query = input.value;
      renderResults();
    });

    container.replaceChildren(input, results);
    renderResults();

    // У этой вкладки нет подписки на store (в отличие от authors/repos/pubs) —
    // результаты поиска не зависят от того, что сейчас выбрано, только от
    // текста запроса, который уже вызывает renderResults() сам по себе.
    return () => {
      // Явных подписок/таймеров не заводили — размонтирование сводится
      // к тому, что mountTabs очищает container через следующий mount().
    };
  },
};
