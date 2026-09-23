import { Theme } from "./theme";

export const darkTheme = new Theme({
  id: "dark",
  name: { ru: "Тёмная", en: "Dark" },
  colorScheme: "dark",
  ui: {
    bg: "#181818",
    surface: "#202020",
    "surface-2": "#2a2a2a",
    border: "#333333",
    text: "#dcdcdc",
    muted: "#8a8a8a",
    accent: "#ff7f50",
    "accent-soft": "rgba(255, 127, 80, 0.18)",
    "danger-bg": "#2a1414",
    "danger-border": "#e03131",
    "danger-text": "#ffb3b3",
  },
  map: {
    background: "#202020",
    dimNode: "rgba(74, 74, 74, 0.45)",
    labelHalo: "#ffffff",
    edge: "rgba(157, 157, 157, 0.35)",
    edgeSelected: "rgba(157, 157, 157, 0.75)",
  },
});
