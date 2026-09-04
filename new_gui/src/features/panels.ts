// Слой "features" — панель с информацией о том, что сейчас выбрано.
// Пока умеет показывать только карточку узла (автор/репозиторий/публикация);
// карточки ребра и департамента, а также сама вкладка "Обзор" из старого
// GUI — отдельные следующие шаги, когда дойдём до параллельных фич.

import type { GraphData } from "../contracts/graph";
import { indexByKey, nodeLabel } from "../core/data";
import type { AppState, Store } from "../core/state";

/** id контейнера в index.html, куда рисуется панель — если его там нет, это ошибка вёрстки, а не тихий сбой. */
const PANEL_ID = "panel";

/**
 * Подключает панель информации: подписывается на Store и перерисовывает
 * содержимое каждый раз, когда меняется state.selection. Возвращает
 * функцию отписки (unmount).
 */
export function mountPanel(store: Store<AppState>, data: GraphData): () => void {
  const maybeContainer = document.getElementById(PANEL_ID);
  if (!maybeContainer) {
    // Явная ошибка вместо тихого "ничего не происходит" — так сразу видно
    // в консоли, что забыли добавить <div id="panel"> в index.html.
    throw new Error(`mountPanel: не найден элемент #${PANEL_ID} в index.html`);
  }
  // Отдельная переменная (не просто снятие "!" с maybeContainer) — TypeScript
  // не переносит сужение типа через null-проверку внутрь функции render,
  // объявленной ниже (замыкание могло бы вызваться в другой момент времени),
  // поэтому нужна новая константа с уже точным типом HTMLElement.
  const container: HTMLElement = maybeContainer;

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

    const dept = deptById.get(node.dept);

    const rows: [string, string][] = [
      ["Ключ", node.key],
      ["Тип", KIND_LABELS[node.kind]],
      ["Департамент", dept ? dept.name : "—"],
    ];
    if (node.kind === "author") rows.push(["Публикаций", String(node.pubs_count)]);
    if (node.kind === "repo") rows.push(["Звёзд", String(node.stars)], ["Владелец", node.owner]);
    if (node.kind === "pub") rows.push(["Год", node.year === null ? "неизвестен" : String(node.year)]);

    container.hidden = false;
    container.replaceChildren(buildCard(nodeLabel(node), rows));
  }

  render(store.get());
  const unsubscribe = store.subscribe(render);
  return unsubscribe;
}

const KIND_LABELS: Record<"author" | "repo" | "pub", string> = {
  author: "Автор",
  repo: "Репозиторий",
  pub: "Публикация",
};

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
