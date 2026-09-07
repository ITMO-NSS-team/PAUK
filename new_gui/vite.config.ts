import { defineConfig } from "vitest/config";

export default defineConfig({
  root: ".",
  // По умолчанию Vite раздаёт статику из папки "public" — здесь она
  // переименована в "private", чтобы не путать её с уже существующим
  // смыслом слова "public" в new_generate (--public в generate_data.py —
  // урезанный, без личных полей, вариант данных для публичного доступа,
  // отдельная задача на будущее). Сейчас new_gui работает только с
  // полным (приватным) вариантом — см. DATA_CONFIG в core/config.ts.
  publicDir: "private",
  test: {
    environment: "jsdom",
    include: ["tests/**/*.test.ts"],
  },
});
