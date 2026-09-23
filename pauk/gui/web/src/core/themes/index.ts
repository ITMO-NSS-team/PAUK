import { darkTheme } from "./dark";
import { lightTheme } from "./light";
import type { Theme } from "./theme";

export { Theme } from "./theme";

/** Все темы в порядке кнопок на меню; первая — тема по умолчанию. */
export const THEMES: readonly Theme[] = [darkTheme, lightTheme];

/**
 * Тема по id; неизвестный id — тема по умолчанию, не падение.
 *
 * @param id - `AppState.theme`.
 */
export function themeById(id: string): Theme {
  return THEMES.find((theme) => theme.id === id) ?? darkTheme;
}
