// Слой "core" — один общий способ отрисовать список карточек вместо того,
// чтобы каждая вкладка заново писала `container.innerHTML += "..."` (именно
// так это было в старом GUI, отдельно в каждом tab-*.js: authors, repos,
// pubs и поиск дублировали один и тот же код создания строки списка).

/**
 * Полностью пересобирает содержимое `container`, отрисовав по одному
 * DOM-элементу на каждый элемент `items` через переданную функцию
 * `renderItem`. Старые дочерние узлы `container` удаляются, новые —
 * добавляются взамен (через `Element.replaceChildren`), в том же порядке,
 * что и `items`.
 *
 * Специально простая реализация — не keyed-diff и не виртуальный DOM.
 * На списках размером в сотни-тысячи элементов (реалистичный масштаб для
 * авторов/публикаций университета) полная пересборка не заметна по
 * производительности, а код при этом на порядок проще, чем сравнение по
 * ключу и переиспользование существующих узлов.
 *
 * # ponytail: полная пересборка на каждый вызов сбрасывает scroll position
 * контейнера при каждом render() (пользователь прокрутил список вниз —
 * любое изменение store откатывает прокрутку наверх). Если это станет
 * реально заметно на живых данных — перейти на keyed-diff (сравнение по
 * стабильному ключу элемента вроде `item.key`, переиспользование уже
 * созданных DOM-узлов вместо их пересоздания), а не раньше.
 *
 * @typeParam T - тип одного элемента списка (например, `AuthorNode` или `SearchHit`).
 * @param container - DOM-элемент, чьё содержимое будет полностью заменено.
 * @param items - данные, которые нужно отрисовать, по одному элементу на строку.
 * @param renderItem - функция, превращающая один элемент `items` в готовый DOM-узел
 *   (обычно `renderListItem` ниже, но может быть и любой другой построитель разметки).
 *
 * @example
 * const container = document.getElementById("tab-content")!;
 * const authors = [{ key: "A1", label: "Иванов И.И." }, { key: "A2", label: "Петрова А.С." }];
 * renderList(container, authors, (author) =>
 *   renderListItem({ label: author.label, onClick: () => console.log(author.key) }),
 * );
 * // container теперь содержит ровно два <button class="tab-list-item">, без остатков
 * // от предыдущего рендера.
 */
export function renderList<T>(container: HTMLElement, items: T[], renderItem: (item: T) => HTMLElement): void {
  container.replaceChildren(...items.map(renderItem));
}

/** Что показать в одной строке списка — общий формат для всех вкладок-списков и поиска, параметр {@link renderListItem}. */
export interface ListItemOptions {
  /** Основной текст строки (класс `.tab-list-item__label`) — например, имя автора или название репозитория. */
  label: string;
  /**
   * Второстепенный текст справа (класс `.tab-list-item__meta`) — например,
   * число публикаций у автора или год у публикации. Без этого поля строка
   * состоит из одного `label`, второй `<span>` вообще не создаётся.
   */
  meta?: string;
  /** Подсвечивать ли строку как выбранную (класс `.tab-list-item--selected`) — true, когда `item.key === state.selection.key`. */
  selected?: boolean;
  /**
   * Значение атрибута `data-kind` на кнопке — нужен только вкладке поиска,
   * чтобы отличать вид результата (`"author"` / `"repo"` / `"pub"` / `"dept"`)
   * и в CSS, и в тестах, не разбирая для этого текст самой строки.
   */
  dataKind?: string;
  /** Обработчик клика по всей строке — обычно пишет выбор в Store и/или подлетает к узлу на карте. */
  onClick: () => void;
}

/**
 * Собирает одну строку списка в едином визуальном формате: кнопка
 * `.tab-list-item` с обязательным `<span class="tab-list-item__label">`
 * внутри и необязательным `<span class="tab-list-item__meta">`, если в
 * `options.meta` что-то передали.
 *
 * Раньше каждая из вкладок (authors/repos/pubs/search) заново писала один
 * и тот же код (`createElement("button")`, два `<span>`, обработчик клика),
 * отличаясь только тем, что именно попадало в label/meta и что происходило
 * по клику. Теперь этот код в одном месте: поменять вёрстку строки списка
 * можно здесь, а не в четырёх файлах разом.
 *
 * @param options - см. {@link ListItemOptions}: что показать в строке и что делать по клику.
 * @returns Готовый `<button type="button" class="tab-list-item">`, ещё не вставленный в DOM —
 *   обычно передаётся дальше в {@link renderList} как результат `renderItem`.
 *
 * @example
 * const button = renderListItem({
 *   label: "Иванов И.И.",
 *   meta: "12",
 *   selected: true,
 *   onClick: () => store.set({ selection: { kind: "node", key: "A1" } }),
 * });
 * // button.outerHTML:
 * // <button type="button" class="tab-list-item tab-list-item--selected">
 * //   <span class="tab-list-item__label">Иванов И.И.</span>
 * //   <span class="tab-list-item__meta">12</span>
 * // </button>
 */
export function renderListItem(options: ListItemOptions): HTMLButtonElement {
  const item = document.createElement("button");
  // type="button" обязателен — иначе клик по кнопке внутри произвольной
  // формы попытался бы её отправить (сабмит); тут формы нет, но задавать
  // type явно — привычка, которая на будущее не даст об это споткнуться.
  item.type = "button";
  item.className = "tab-list-item";
  if (options.selected) item.classList.add("tab-list-item--selected");
  if (options.dataKind) item.dataset.kind = options.dataKind;

  const label = document.createElement("span");
  label.className = "tab-list-item__label";
  label.textContent = options.label;
  item.appendChild(label);

  if (options.meta) {
    const meta = document.createElement("span");
    meta.className = "tab-list-item__meta";
    meta.textContent = options.meta;
    item.appendChild(meta);
  }

  item.addEventListener("click", options.onClick);
  return item;
}
