// Слой "features" — экран загрузки (boot-progress) и меню (полноэкранный
// выбор языка + один вход в приложение). Boot-экран виден с первого кадра,
// пока грузится graph-data.json. Меню — не одноразовый онбординг с
// localStorage-флагом, а настоящее состояние приложения
// (AppState.screen === "menu", см. core/state.ts): показывается на КАЖДОЙ
// загрузке страницы БЕЗУСЛОВНО, даже если в адресной строке была
// сохранённая ссылка на конкретный узел (app/main.ts не читает URL при
// начальной загрузке вообще, см. там же) — прямая просьба, и на него
// всегда можно вернуться кликом по заголовку "PAUK" в шапке сайдбара.

import { requireElement } from "../core/dom";
import { t, type LocaleKey } from "../core/i18n";
import type { AppState, Store } from "../core/state";

/** Стадия загрузки, которую показывает boot-экран. */
export type BootStage = "loading" | "rendering" | "error";

/** Ключ статус-строки boot-экрана для каждой стадии. */
const STAGE_LOCALE_KEY: Record<BootStage, LocaleKey> = {
  loading: "start.loading",
  rendering: "start.rendering",
  error: "start.error",
};

/**
 * Доля заполнения прогресс-бара для стадий loading/rendering — по
 * известным контрольным точкам, а не побайтовым отслеживанием
 * `ReadableStream`, как в старом GUI: то же ощущение "грузится →
 * рисуется → готово" заметно меньшим кодом. У стадии "error" своей доли
 * нет — бар просто остаётся там, где остановился, а не скачет на 100%
 * при сбое.
 */
const STAGE_PROGRESS: Record<"loading" | "rendering", number> = {
  loading: 15,
  rendering: 70,
};

/**
 * Подключает boot-экран и меню.
 *
 * @param store - Store приложения — меню читает `screen`/`lang` для
 *   показа/скрытия себя и приложения и для перерисовки своего текста, и
 *   пишет `screen`/`tab`/`lang` по клику на свои кнопки.
 * @returns `setBootStage`/`finishBoot` — вызывать из app/main.ts по ходу загрузки данных.
 */
export function mountStart(store: Store<AppState>): {
  /** Обновляет прогресс-бар и статус-текст boot-экрана. */
  setBootStage: (stage: BootStage) => void;
  /**
   * Прячет boot-экран. Что показать дальше (меню или обычный интерфейс) —
   * уже решено `store.screen` (выставлен из `parseUrlState()` в
   * app/main.ts ДО вызова этой функции) — здесь решать нечего.
   */
  finishBoot: () => void;
} {
  const boot = requireElement("boot-screen");
  const bar = requireElement("boot-progress-bar");
  const status = requireElement("boot-status");
  const menu = requireElement("menu");
  const app = requireElement("app");
  const brand = requireElement("brand");
  const badge = requireElement("menu-badge-text");
  const title = requireElement("menu-title");
  const subtitle = requireElement("menu-subtitle");
  const enterButton = requireElement("menu-enter");
  const langButtons = menu.querySelectorAll<HTMLButtonElement>("button[data-lang]");

  function setBootStage(stage: BootStage): void {
    if (stage !== "error") bar.style.width = `${STAGE_PROGRESS[stage]}%`;
    status.textContent = t(STAGE_LOCALE_KEY[stage], store.get().lang);
  }

  function finishBoot(): void {
    bar.style.width = "100%";
    // Небольшая задержка перед скрытием — та же идея, что и в старом GUI:
    // дать полосе прогресса реально долистать до конца, а не мигнуть
    // мгновенно из "70%" в "нет экрана вообще".
    setTimeout(() => {
      boot.hidden = true;
    }, 200);
  }

  /** Перерисовывает меню (текст + видимость меню/приложения) под текущее состояние. */
  function render(state: AppState): void {
    const isMenu = state.screen === "menu";
    menu.hidden = !isMenu;
    app.hidden = isMenu;

    const lang = state.lang;
    badge.textContent = t("start.badge", lang);
    title.textContent = t("start.title", lang);
    subtitle.textContent = t("start.subtitle", lang);
    enterButton.textContent = t("start.cta", lang);
    for (const button of langButtons) {
      button.classList.toggle("menu-lang--active", button.dataset.lang === lang);
    }
  }

  // Один вход в приложение, всегда на первую вкладку — отдельные кнопки
  // выбора вкладки на меню убраны по прямой просьбе. Устаревший выбор (с
  // совсем другой вкладки, если до захода в меню была выбрана, например,
  // публикация) сама обнулит map/build.ts::mountReactiveGraph — она видит
  // смену store.tab независимо от того, что именно её вызвало.
  enterButton.addEventListener("click", () => {
    store.set({ screen: "app", tab: 1 });
  });
  for (const button of langButtons) {
    button.addEventListener("click", () => {
      store.set({ lang: button.dataset.lang as AppState["lang"] });
    });
  }
  // Единственный способ вернуться в меню из приложения — заголовок сайдбара.
  brand.addEventListener("click", () => {
    store.set({ screen: "menu" });
  });

  render(store.get());
  store.subscribe(render);

  return { setBootStage, finishBoot };
}
