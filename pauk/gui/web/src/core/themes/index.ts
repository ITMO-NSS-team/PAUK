import { darkTheme } from "./dark";
import { lightTheme } from "./light";
import type { Theme } from "./theme";

export { Theme } from "./theme";

export const THEMES: readonly Theme[] = [darkTheme, lightTheme];

export function themeById(id: string): Theme {
  return THEMES.find((theme) => theme.id === id) ?? darkTheme;
}
