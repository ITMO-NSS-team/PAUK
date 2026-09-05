// @ts-check
import { resolve } from "node:path";
import js from "@eslint/js";
import tseslint from "typescript-eslint";
import boundaries from "eslint-plugin-boundaries";
import prettierConfig from "eslint-config-prettier";

// Layer order, lowest first. A layer may only import from layers at or
// below its own position — this is the one hard rule new_gui replaces
// 168 unscoped globals with (see ../new-gui-blueprint.html section 3).
const LAYERS = ["contracts", "core", "map", "features", "app"];

export default tseslint.config(
  {
    ignores: ["dist/**", "node_modules/**"],
  },
  js.configs.recommended,
  ...tseslint.configs.strict,
  ...tseslint.configs.stylistic,
  {
    files: ["src/**/*.ts"],
    plugins: { boundaries },
    settings: {
      "boundaries/root-path": resolve(import.meta.dirname),
      "import/resolver": {
        typescript: { alwaysTryTypes: true, project: "./tsconfig.json" },
      },
      "boundaries/elements": LAYERS.map((type) => ({
        type,
        pattern: `src/${type}/**`,
        partialMatch: false,
      })),
    },
    rules: {
      "boundaries/dependencies": [
        "error",
        {
          default: "disallow",
          policies: LAYERS.map((type, index) => ({
            from: { element: { type } },
            allow: { to: { element: { type: LAYERS.slice(0, index + 1) } } },
          })),
        },
      ],
    },
  },
  prettierConfig,
);
