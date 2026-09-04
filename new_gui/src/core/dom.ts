/**
 * Ищет обязательный элемент разметки по id или явно падает с понятной
 * ошибкой. Используется вместо голого document.getElementById там, где
 * отсутствие элемента — это ошибка вёрстки (забыли добавить <div> в
 * index.html), а не нормальный сценарий "элемента может не быть" — лучше
 * сразу увидеть исключение в консоли, чем молча ничего не отрисовать.
 */
export function requireElement(id: string): HTMLElement {
  const element = document.getElementById(id);
  if (!element) throw new Error(`не найден элемент #${id} в index.html`);
  return element;
}
