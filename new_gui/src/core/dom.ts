// Слой "core" — маленькие DOM-утилиты общего назначения, не привязанные ни
// к одной конкретной фиче (в отличие от core/render.ts, который умеет
// рисовать именно списки карточек вкладок).

/**
 * Ищет обязательный элемент разметки по id и возвращает его как обычный
 * `HTMLElement`. Если элемента нет в документе — не возвращает `null`, как
 * это делает голый `document.getElementById()`, а сразу бросает исключение
 * с понятным текстом.
 *
 * Используется во всех местах, где отсутствие элемента — это ошибка вёрстки
 * (например, кто-то удалил `<div id="panel">` из index.html или опечатался
 * в id), а не нормальный рабочий сценарий "элемента может не быть". Явное
 * исключение прямо в момент монтирования фичи гораздо понятнее для отладки,
 * чем молчаливый `null`, который потом всплывёт где-то в глубине рендера
 * как "Cannot read properties of null".
 *
 * @param id - id искомого элемента, без символа "#" (как в атрибуте
 *   `id="..."` в HTML, а не как в CSS-селекторе).
 * @returns Найденный DOM-элемент. Тип — просто `HTMLElement` (не `null`),
 *   поэтому вызывающему коду не нужна проверка на `null` после вызова.
 * @throws Error, если элемента с таким id нет в текущем документе.
 *
 * @example
 * // Голый DOM API вернул бы `HTMLElement | null`, и пришлось бы либо
 * // проверять null в каждом месте использования, либо ставить `!`:
 * const panel = document.getElementById("panel"); // HTMLElement | null
 *
 * // requireElement сразу даёт гарантированный HTMLElement:
 * const panel = requireElement("panel");
 * panel.hidden = false; // без проверки на null и без `!`
 */
export function requireElement(id: string): HTMLElement {
  const element = document.getElementById(id);
  if (!element) throw new Error(`не найден элемент #${id} в index.html`);
  return element;
}
