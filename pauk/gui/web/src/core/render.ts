/**
 * Replaces all children of `container`.
 *
 * TODO: a full rebuild resets the scroll position on every render; switch to
 * a keyed diff if that becomes noticeable.
 */
export function renderList<T>(container: HTMLElement, items: T[], renderItem: (item: T) => HTMLElement): void {
  container.replaceChildren(...items.map(renderItem));
}

export interface ListItemOptions {
  label: string;
  meta?: string;
  selected?: boolean;
  /** Result kind for CSS and tests (search results only). */
  dataKind?: string;
  onClick: () => void;
}

export function renderListItem(options: ListItemOptions): HTMLButtonElement {
  const item = document.createElement("button");
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
