// Одна тема оформления — самодостаточный объект: имя, CSS-токены вёрстки и
// цвета карты. Все темы собраны в themes/index.ts; новая тема — новый файл с
// `new Theme({...})` и одна строка в THEMES, остальной код про конкретные
// темы ничего не знает.

/** CSS-переменные вёрстки (`var(--bg)` и т.д. в index.html). Акцент — общий для всех тем. */
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

/** Цвета canvas-карты (Sigma), которые не выразить CSS-переменными. */
export interface ThemeMapColors {
  /** Фон карты — совпадает с `--surface`, чтобы сайдбар и карта читались одним тоном. */
  background: string;
  /** Узел не в фокусе и не сосед фокуса — полупрозрачный, "отошёл на фон". */
  dimNode: string;
  /** Обводка вокруг цветной подписи узла и названия региона. */
  labelHalo: string;
  edge: string;
  edgeSelected: string;
}

export interface ThemeOptions {
  /** Стабильный id — хранится в `AppState.theme`. */
  id: string;
  name: { ru: string; en: string };
  /** Для нативных элементов браузера (скроллбары, поля ввода). */
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

  /**
   * Применяет тему к документу: CSS-переменные и `color-scheme` на `root`.
   * Цвета карты отсюда не трогаются — их читает сама карта при отрисовке.
   *
   * @param root - обычно `document.documentElement`.
   */
  apply(root: HTMLElement): void {
    for (const [token, value] of Object.entries(this.ui))
      root.style.setProperty(`--${token}`, value);
    root.style.colorScheme = this.colorScheme;
  }
}
