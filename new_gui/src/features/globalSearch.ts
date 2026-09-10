// Слой "features" — окно поиска по ВСЕМ видам сущностей сразу (авторы,
// репозитории, публикации, департаменты), поверх всего интерфейса, а не
// вкладка. Единственный способ найти департамент вообще: у него нет ни
// своей вкладки, ни узла-кнопки на карте (только невидимый якорь подписи,
// см. map/build.ts::addDeptLabelAnchors) — на реальных данных департаментов
// около 60, найти нужный "на глаз" по подписям на карте нереально.

import type { GraphData, PubDetail, RepoDetail } from "../contracts/graph";
import { SEARCH_CONFIG } from "../core/config";
import { requireElement } from "../core/dom";
import { t } from "../core/i18n";
import { renderList, renderListItem } from "../core/render";
import { TAB_FOR_KIND, type AppState, type Store } from "../core/state";
import { buildSearchIndex, parseDeptHitKey, searchHits } from "./search";

/**
 * Подключает глобальный поиск. Открывается кнопкой `#global-search-trigger`
 * в сайдбаре или клавишей `"/"` (если фокус не в текстовом поле — иначе
 * "/" печаталась бы как обычный символ), закрывается `Escape` или кликом
 * по затемнённому фону. Работает из ЛЮБОГО экрана (`AppState.screen`) —
 * `#global-search` лежит вне `#app`/`#menu` с более высоким z-index, см. index.html.
 *
 * Клик по результату пишет `store.selection` (и `store.tab`/`screen`, если
 * нужно переключить их под вид найденной сущности) — та же механика "выбор
 * = навигация", что и у клика по узлу на карте (features/selection.ts) или
 * по строке списка вкладки (features/tabs/nodeListTab.ts): камера подлетает
 * к узлу централизованно в map/build.ts::mountReactiveGraph.
 *
 * @param store - Store приложения.
 * @param data - данные графа.
 * @param pubDetails - карта деталей публикаций (для настоящих названий публикаций в результатах).
 * @param repoDetails - карта описаний/владельцев/ссылок репозиториев (для короткого пути на GitHub в результатах).
 * @returns Функция отписки (unmount) — снимает обработчик клавиатуры.
 */
export function mountGlobalSearch(
  store: Store<AppState>,
  data: GraphData,
  pubDetails: Map<string, PubDetail>,
  repoDetails: Map<string, RepoDetail>,
): () => void {
  const trigger = requireElement("global-search-trigger");
  const overlay = requireElement("global-search");
  const input = requireElement("global-search-input") as HTMLInputElement;
  const resultsEl = requireElement("global-search-results");

  // Индекс — снимок текущего языка на момент ОТКРЫТИЯ окна, не пересчитывается
  // непрерывно, пока окно закрыто (когда его вообще никто не видит, смена
  // языка в другом месте интерфейса не обязана его касаться).
  let index = buildSearchIndex(data, store.get().lang, pubDetails, repoDetails);

  /** Подпись кнопки-триггера в сайдбаре — реагирует на смену языка, в отличие от текста внутри самого (обычно закрытого) окна поиска. */
  function renderTrigger(lang: AppState["lang"]): void {
    const hint = document.createElement("span");
    hint.className = "global-search-trigger__hint";
    hint.textContent = "/";
    trigger.replaceChildren(t("search.trigger", lang), hint);
  }
  renderTrigger(store.get().lang);
  const unsubscribeTrigger = store.subscribe((state) => renderTrigger(state.lang));

  /** Перерисовывает список результатов под текущий текст в поле ввода. */
  function render(): void {
    const hits = searchHits(index, input.value);

    if (input.value.trim() && hits.length === 0) {
      const empty = document.createElement("div");
      empty.className = "tab-empty";
      empty.textContent = t("tab.noResults", store.get().lang);
      resultsEl.replaceChildren(empty);
      return;
    }

    renderList(resultsEl, hits.slice(0, SEARCH_CONFIG.resultsLimit), (hit) =>
      renderListItem({
        label: hit.label,
        meta: hit.sub ?? undefined,
        dataKind: hit.kind,
        onClick: () => {
          // screen: "app" безусловно — поиск открыт и с меню (см. клавиша
          // "/" ниже), результат должен привести в приложение, а не просто
          // молча записаться в состояние, скрытое за #menu.
          if (hit.kind === "dept") {
            store.set({ screen: "app", selection: { kind: "dept", id: parseDeptHitKey(hit.key) } });
          } else {
            store.set({ screen: "app", tab: TAB_FOR_KIND[hit.kind], selection: { kind: "node", key: hit.key } });
          }
          close();
        },
      }),
    );
  }

  function open(): void {
    const lang = store.get().lang;
    index = buildSearchIndex(data, lang, pubDetails, repoDetails);
    input.placeholder = t("search.placeholder", lang);
    overlay.hidden = false;
    input.value = "";
    render();
    input.focus();
  }

  function close(): void {
    overlay.hidden = true;
  }

  input.addEventListener("input", render);
  trigger.addEventListener("click", open);
  // Клик именно по затемнённому фону (event.target === overlay), а не по
  // самому окну поиска внутри него — те клики доходят с target глубже внутри.
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) close();
  });

  /**
   * `"/"` открывает поиск из любого места интерфейса (кроме текстовых
   * полей — иначе перехватывало бы обычный ввод символа "/"), `Escape`
   * закрывает уже открытое окно.
   */
  function onKeydown(event: KeyboardEvent): void {
    if (!overlay.hidden && event.key === "Escape") {
      close();
      return;
    }
    const target = event.target as HTMLElement;
    const typing = target.tagName === "INPUT" || target.tagName === "TEXTAREA";
    if (overlay.hidden && event.key === "/" && !typing) {
      event.preventDefault();
      open();
    }
  }
  document.addEventListener("keydown", onKeydown);

  return () => {
    document.removeEventListener("keydown", onKeydown);
    unsubscribeTrigger();
  };
}
