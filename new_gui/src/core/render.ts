// Один общий способ отрисовать список карточек — вместо того чтобы каждая
// вкладка заново писала container.innerHTML += "..." (именно так это было
// в старом GUI, отдельно в каждом tab-*.js).

/**
 * Полностью пересобирает содержимое container из items. Специально
 * простая реализация (не keyed-diff/виртуальный DOM) — на списках в
 * сотни-тысячи элементов пересборка не заметна по производительности.
 *
 * # ponytail: полная пересборка на каждый вызов сбрасывает scroll
 * position контейнера при каждом render(). Если это станет заметно на
 * реальных данных — перейти на keyed-diff (сравнение по стабильному
 * ключу элемента, переиспользование уже созданных DOM-узлов вместо
 * пересоздания), а не раньше.
 */
export function renderList<T>(container: HTMLElement, items: T[], renderItem: (item: T) => HTMLElement): void {
  container.replaceChildren(...items.map(renderItem));
}

/** Что показать в одной строке списка — общий формат для всех вкладок-списков и поиска. */
export interface ListItemOptions {
  /** Основной текст строки (класс .tab-list-item__label). */
  label: string;
  /** Второстепенный текст справа (класс .tab-list-item__meta) — например, число публикаций или год. Без него строка состоит из одного label. */
  meta?: string;
  /** Подсвечивать ли строку как выбранную (класс .tab-list-item--selected). */
  selected?: boolean;
  /** data-kind на кнопке — нужен только вкладке поиска, чтобы отличать вид результата (author/repo/pub/dept) без парсинга текста. */
  dataKind?: string;
  onClick: () => void;
}

/**
 * Собирает одну строку списка в едином визуальном формате — раньше каждая
 * из вкладок (authors/repos/pubs/search) заново писала один и тот же код
 * (createElement("button"), два <span>, обработчик клика), отличаясь
 * только тем, что именно попадало в label/meta и что происходило по
 * клику. Теперь этот код в одном месте: поменять вёрстку строки списка
 * можно здесь, а не в четырёх файлах разом.
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
