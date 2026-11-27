export interface ThemeUiTokens {
  bg: string;
  surface: string;
  "surface-2": string;
  border: string;
  text: string;
  muted: string;
  accent: string;
  "accent-fill": string;
  "accent-soft": string;
  "danger-bg": string;
  "danger-border": string;
  "danger-text": string;
}

export interface ThemeMapColors {
  background: string;
  dimNode: string;
  labelHalo: string;
  edge: string;
  edgeSelected: string;
}

export interface ThemeOptions {
  id: string;
  name: { ru: string; en: string };
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

  apply(root: HTMLElement): void {
    for (const [token, value] of Object.entries(this.ui))
      root.style.setProperty(`--${token}`, value);
    root.style.colorScheme = this.colorScheme;
  }
}
