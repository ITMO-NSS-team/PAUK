import type { Map as MapLibreMap } from "maplibre-gl";
import type { GraphData } from "../contracts/graph";
import { requireElement } from "../core/dom";
import { t } from "../core/i18n";
import type { AppState, Store } from "../core/state";
import { refreshGraphForTab } from "../map/build";

/**
 * Кнопка переключения языка — единственное место в приложении, которое
 * пишет в state.lang. Все остальные фичи (панель, вкладки, карта) только
 * читают lang из подписки на Store, не знают друг о друге и не хранят
 * язык у себя — источник правды один.
 */
export function mountLangToggle(store: Store<AppState>, map: MapLibreMap, data: GraphData): () => void {
  const button = requireElement("lang-toggle");

  function render(state: AppState): void {
    // Кнопка показывает язык, НА КОТОРЫЙ переключит клик, а не текущий —
    // так и вело себя старое переключение ru/en в старом GUI.
    button.textContent = t("lang.toggle", state.lang);
  }

  function onClick(): void {
    const nextLang = store.get().lang === "ru" ? "en" : "ru";
    store.set({ lang: nextLang });
    // properties узлов/рёбер на карте не обновляются сами через подписку
    // на Store (в отличие от DOM-фич) — это отдельные GeoJSON-источники,
    // их нужно пересобрать явно, для той же вкладки, что активна сейчас.
    refreshGraphForTab(map, data, nextLang, store.get().tab);
  }

  button.addEventListener("click", onClick);
  render(store.get());
  const unsubscribe = store.subscribe(render);

  return () => {
    button.removeEventListener("click", onClick);
    unsubscribe();
  };
}
