// Слой "features" — панель с информацией о том, что сейчас выбрано.
// Умеет показывать карточку узла (автор/репозиторий/публикация), ребра
// (кто с кем связан и с каким весом) и департамента (сводные числа из
// самого Department). Вкладка "Обзор" из старого GUI (что показывается,
// когда вообще ничего не выбрано) — отдельный будущий шаг.

import type { GraphData } from "../contracts/graph";
import type { SearchDetail } from "../contracts/search";
import { indexByKey, nodeLabel } from "../core/data";
import { requireElement } from "../core/dom";
import { kindLabel, localize, t } from "../core/i18n";
import type { AppState, Store } from "../core/state";

/**
 * Подключает панель информации: подписывается на Store и перерисовывает
 * содержимое каждый раз, когда меняется state.selection ИЛИ state.lang —
 * оба поля читаются в одном render(), поэтому одной подписки достаточно,
 * без отдельной логики "что именно изменилось". Возвращает функцию
 * отписки (unmount).
 */
export function mountPanel(
  store: Store<AppState>,
  data: GraphData,
  searchDetails: Map<string, SearchDetail>,
): () => void {
  const container = requireElement("panel");

  // Строится один раз при монтировании, а не на каждый рендер — поиск по
  // ключу должен быть мгновенным, а не пересчитывать индекс на каждый клик.
  const index = indexByKey(data);
  const deptById = new Map(data.departments.map((dept) => [dept.id, dept]));

  function hide(): void {
    container.hidden = true;
    container.replaceChildren();
  }

  /** Только textContent — никакого innerHTML, данные из графа не должны интерпретироваться как разметка. */
  function show(title: string, rows: [string, string][]): void {
    container.hidden = false;
    container.replaceChildren(buildCard(title, rows));
  }

  /** Пересобирает содержимое панели под текущее состояние — один из трёх видов карточки либо ничего, если ничего не выбрано. */
  function render(state: AppState): void {
    const { selection, lang } = state;
    if (selection === null) return hide();

    if (selection.kind === "node") {
      const node = index.get(selection.key);
      // Ключ выбран, но узла с таким ключом нет в текущих данных — такое
      // не должно происходить (клик по карте берёт key прямо из тех же
      // данных), но если вдруг случится рассинхрон, лучше молча спрятать
      // панель, чем показать пустую карточку.
      if (!node) return hide();

      const dept = deptById.get(node.dept);
      const rows: [string, string][] = [
        [t("field.key", lang), node.key],
        [t("field.kind", lang), kindLabel(node.kind, lang)],
        [t("field.dept", lang), dept ? localize(dept.name, dept.name_en, lang) : t("field.unknownDept", lang)],
      ];
      if (node.kind === "author") rows.push([t("field.pubsCount", lang), String(node.pubs_count)]);
      if (node.kind === "repo") {
        rows.push([t("field.stars", lang), String(node.stars)], [t("field.owner", lang), node.owner]);
      }
      if (node.kind === "pub") {
        rows.push([t("field.year", lang), node.year === null ? t("field.yearUnknown", lang) : String(node.year)]);
      }

      return show(nodeLabel(node, lang, searchDetails), rows);
    }

    if (selection.kind === "edge") {
      const from = index.get(selection.s);
      const to = index.get(selection.t);
      if (!from || !to) return hide();

      const rows: [string, string][] = [
        [t("field.edgeFrom", lang), nodeLabel(from, lang, searchDetails)],
        [t("field.edgeTo", lang), nodeLabel(to, lang, searchDetails)],
        [t("field.edgeWeight", lang), String(selection.w)],
      ];
      return show(t("kind.edge", lang), rows);
    }

    // selection.kind === "dept"
    const dept = deptById.get(selection.id);
    if (!dept) return hide();

    const rows: [string, string][] = [
      [t("field.authorsCount", lang), String(dept.n_authors)],
      [t("field.pubsCount", lang), String(dept.n_pubs)],
      [t("field.reposCount", lang), String(dept.n_repos)],
      [t("field.total", lang), String(dept.n)],
    ];
    return show(localize(dept.name, dept.name_en, lang), rows);
  }

  render(store.get());
  const unsubscribe = store.subscribe(render);
  return unsubscribe;
}

/** Собирает DOM-карточку из заголовка и списка пар "подпись — значение", без единой строки innerHTML. */
function buildCard(title: string, rows: [string, string][]): HTMLElement {
  const card = document.createElement("div");
  card.className = "panel-card";

  const heading = document.createElement("h3");
  heading.textContent = title;
  card.appendChild(heading);

  const list = document.createElement("dl");
  for (const [label, value] of rows) {
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value;
    list.append(dt, dd);
  }
  card.appendChild(list);

  return card;
}
