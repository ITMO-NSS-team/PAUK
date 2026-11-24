import { Theme } from "./theme";

export const lightTheme = new Theme({
  id: "light",
  name: { ru: "Светлая", en: "Light" },
  colorScheme: "light",
  ui: {
    bg: "#e9e9e6",
    surface: "#f6f6f4",
    "surface-2": "#ebebe8",
    border: "#d4d4cf",
    text: "#1f1f1f",
    muted: "#6e6e6e",
    accent: "#5b428a",
    "accent-fill": "#695199",
    "accent-soft": "rgba(91, 66, 138, 0.10)",
    "danger-bg": "#fdecec",
    "danger-border": "#e03131",
    "danger-text": "#a61e1e",
  },
  map: {
    background: "#f6f6f4",
    dimNode: "rgba(74, 74, 74, 0.3)",
    labelHalo: "#ffffff",
    edge: "rgba(60, 60, 60, 0.5)",
    edgeSelected: "rgba(30, 30, 30, 0.85)",
  },
});
