import { requireElement } from "../core/dom";
import { t } from "../core/i18n";
import type { AppState, Store } from "../core/state";

/**
 * Кнопка переключения языка — единственное место в приложении, которое
 * пишет в state.lang. Все остальные фичи (панель, вкладки, карта через
 * map/build.ts::mountReactiveGraph) сами следят за store.lang и
 * перерисовываются — эта функция ничего, кроме store.set(), не делает,
 * и поэтому ей не нужны ни map, ни data.
 */
export function mountLangToggle(store: Store<AppState>): () => void {
  const button = requireElement("lang-toggle");

  function render(state: AppState): void {
    // Кнопка показывает язык, НА КОТОРЫЙ переключит клик, а не текущий —
    // так и вело себя старое переключение ru/en в старом GUI.
    button.textContent = t("lang.toggle", state.lang);
  }

  function onClick(): void {
    store.set({ lang: store.get().lang === "ru" ? "en" : "ru" });
  }

  button.addEventListener("click", onClick);
  render(store.get());
  const unsubscribe = store.subscribe(render);

  return () => {
    button.removeEventListener("click", onClick);
    unsubscribe();
  };
}
