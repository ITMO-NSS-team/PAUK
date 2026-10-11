// A new theme is one file with `new Theme({...})` plus one line in THEMES.

/** CSS variables used by index.html (`var(--bg)` etc.). */
export interface ThemeUiTokens {
  bg: string;
  surface: string;
  "surface-2": string;
  border: string;
  text: string;
  muted: string;
  accent: string;
  "accent-soft": string;
  "danger-bg": string;
  "danger-border": string;
  "danger-text": string;
}

/** Sigma draws on canvas, so the map cannot read CSS variables. */
export interface ThemeMapColors {
  background: string;
  /** Node outside the focus and its neighbours. */
  dimNode: string;
  labelHalo: string;
  edge: string;
  edgeSelected: string;
}

export interface ThemeOptions {
  id: string;
  name: { ru: string; en: string };
  /** For native browser widgets (scrollbars, inputs). */
  colorScheme: "dark" | "light";
  ui: ThemeUiTokens;
  map: ThemeMapColors;
}

export class Theme {
  readonly id: string;
  readonly name: ThemeOptions["name"];
  readonly colorScheme: ThemeOptions["colorScheme"];
  readonly ui: ThemeUiTokens;
  readonly map: ThemeMapColors;

  constructor(options: ThemeOptions) {
    this.id = options.id;
    this.name = options.name;
    this.colorScheme = options.colorScheme;
    this.ui = options.ui;
    this.map = options.map;
  }

  /** Map colors are not applied here: the map reads them at render time. */
  apply(root: HTMLElement): void {
    for (const [token, value] of Object.entries(this.ui))
      root.style.setProperty(`--${token}`, value);
    root.style.colorScheme = this.colorScheme;
  }
}
