// Слой "features" — панель с информацией о том, что сейчас выбрано.
// Пока умеет показывать только карточку узла (автор/репозиторий/публикация);
// карточки ребра и департамента, а также сама вкладка "Обзор" из старого
// GUI — отдельные следующие шаги, когда дойдём до параллельных фич.

import type { GraphData } from "../contracts/graph";
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
export function mountPanel(store: Store<AppState>, data: GraphData): () => void {
  const container = requireElement("panel");

  // Строится один раз при монтировании, а не на каждый рендер — поиск по
  // ключу должен быть мгновенным, а не пересчитывать индекс на каждый клик.
  const index = indexByKey(data);
  const deptById = new Map(data.departments.map((dept) => [dept.id, dept]));

  /** Полностью пересобирает содержимое панели под текущее состояние. Только textContent — никакого innerHTML, данные из графа не должны интерпретироваться как разметка. */
  function render(state: AppState): void {
    if (state.selection === null || state.selection.kind !== "node") {
      container.hidden = true;
      container.replaceChildren();
      return;
    }

    const node = index.get(state.selection.key);
    if (!node) {
      // Ключ выбран, но узла с таким ключом нет в текущих данных — такое
      // не должно происходить (клик по карте берёт key прямо из тех же
      // данных), но если вдруг случится рассинхрон, лучше молча спрятать
      // панель, чем показать пустую карточку.
      container.hidden = true;
      container.replaceChildren();
      return;
    }

    const { lang } = state;
    const dept = deptById.get(node.dept);

    const rows: [string, string][] = [
      [t("field.key", lang), node.key],
      [t("field.kind", lang), kindLabel(node.kind, lang)],
      [t("field.dept", lang), dept ? localize(dept.name, dept.name_en, lang) : t("field.unknownDept", lang)],
    ];
    if (node.kind === "author") rows.push([t("field.pubsCount", lang), String(node.pubs_count)]);
    if (node.kind === "repo") rows.push([t("field.stars", lang), String(node.stars)], [t("field.owner", lang), node.owner]);
    if (node.kind === "pub") {
      rows.push([t("field.year", lang), node.year === null ? t("field.yearUnknown", lang) : String(node.year)]);
    }

    container.hidden = false;
    container.replaceChildren(buildCard(nodeLabel(node, lang), rows));
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
