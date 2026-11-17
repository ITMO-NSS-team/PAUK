import type { NodeKind } from "../contracts/graph";
import type { DetailResource } from "../core/details";
import { requireElement } from "../core/dom";
import { kindLabel, t } from "../core/i18n";
import type { AppState, Store } from "../core/state";

/** Errors stay visible even when no card is selected. */
export function mountDetailErrors(
  store: Store<AppState>,
  resources: Record<NodeKind, DetailResource>,
): () => void {
  const container = requireElement("detail-errors");
  function render(): void {
    const lang = store.get().lang;
    const errors = Object.entries(resources).filter(([, resource]) => resource.status === "failed");
    container.hidden = errors.length === 0;
    container.replaceChildren(
      ...errors.map(([kind, resource]) => {
        const row = document.createElement("div");
        row.append(`${kindLabel(kind as NodeKind, lang)}: ${t("details.failed", lang)} `);
        const retry = document.createElement("button");
        retry.type = "button";
        retry.textContent = t("details.retry", lang);
        retry.addEventListener("click", () => {
          void resource.load();
        });
        row.append(retry);
        return row;
      }),
    );
  }
  render();
  return store.subscribe(render);
}
