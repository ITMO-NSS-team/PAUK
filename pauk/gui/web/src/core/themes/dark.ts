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
    accent: "#ff913d",
    "accent-fill": "#ff913d",
    "accent-soft": "rgba(255, 145, 61, 0.18)",
    "danger-bg": "#2a1414",
    "danger-border": "#e03131",
    "danger-text": "#ffb3b3",
  },
  map: {
    background: "#202020",
    dimNode: "rgba(74, 74, 74, 0.3)",
    labelHalo: "#202020",
    edge: "rgba(92, 92, 92, 0.28)",
    edgeSelected: "rgba(135, 135, 135, 0.80)",
  },
});
