import { Theme } from "./theme";

// Та же иерархия, что у тёмной: bg чуть темнее surface, surface-2 для
// полей и кнопок, muted для второстепенного текста; акцент тот же.
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
    accent: "#ff7f50",
    "accent-soft": "rgba(255, 127, 80, 0.16)",
    "danger-bg": "#fdecec",
    "danger-border": "#e03131",
    "danger-text": "#a61e1e",
  },
  map: {
    background: "#f6f6f4",
    dimNode: "rgba(74, 74, 74, 0.45)",
    labelHalo: "#ffffff",
    edge: "rgba(60, 60, 60, 0.5)",
    edgeSelected: "rgba(30, 30, 30, 0.85)",
  },
});
