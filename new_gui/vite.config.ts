import { defineConfig } from "vitest/config";

export default defineConfig({
  root: ".",
  // Vite-каталог статики указывает на общее дерево данных репозитория
  // (<repo_root>/data/gui/{private,public}/ — та же пара, что и в
  // pauk.settings.Settings.gui_dir и new_generate/generate_data.py
  // --out-dir), а не на папку внутри new_gui/. "publicDir" здесь — это имя
  // опции самого Vite (место, откуда раздаётся статика), не намёк на то,
  // какой из двух data-вариантов сейчас читаем: путь ниже ведёт именно в
  // private/ — полный вариант с personal-полями; public/ (урезанный, без
  // них, для сборки вне корпоративной сети) появится отдельной задачей,
  // когда до него дойдём — см. DATA_CONFIG в core/config.ts.
  publicDir: "../data/gui/private",
  test: {
    environment: "jsdom",
    include: ["tests/**/*.test.ts"],
  },
});
