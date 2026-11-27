export function requireElement(id: string): HTMLElement {
  const element = document.getElementById(id);
  if (!element) throw new Error(`element #${id} not found in index.html`);
  return element;
}

export function createLoadingIndicator(): HTMLElement {
  const span = document.createElement("span");
  span.className = "loading-indicator";
  span.setAttribute("role", "status");
  span.setAttribute("aria-label", "загрузка");
  return span;
}

export function showLoadError(message: string): void {
  const loadError = requireElement("load-error");
  loadError.textContent = message;
  loadError.hidden = false;
}
