import { darkTheme } from "./dark";
import { lightTheme } from "./light";
import type { Theme } from "./theme";

export { Theme } from "./theme";

/** Menu button order; the first one is the default. */
export const THEMES: readonly Theme[] = [darkTheme, lightTheme];

/** Unknown id falls back to the default theme. */
export function themeById(id: string): Theme {
  return THEMES.find((theme) => theme.id === id) ?? darkTheme;
}
