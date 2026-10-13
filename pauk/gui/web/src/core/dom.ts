/** @throws Error when the element is missing from index.html. */
export function requireElement(id: string): HTMLElement {
  const element = document.getElementById(id);
  if (!element) throw new Error(`element #${id} not found in index.html`);
  return element;
}

/** Placeholder shown until a `*-detail.json` arrives. */
export function createLoadingIndicator(): HTMLElement {
  const span = document.createElement("span");
  span.className = "loading-indicator";
  span.setAttribute("role", "status");
  span.setAttribute("aria-label", "загрузка");
  return span;
}

/** Fatal error banner: nothing else can be drawn. */
export function showLoadError(message: string): void {
  const loadError = requireElement("load-error");
  loadError.textContent = message;
  loadError.hidden = false;
}
