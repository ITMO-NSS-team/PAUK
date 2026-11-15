import { defineConfig } from "vitest/config";

export default defineConfig(({ mode }) => {
  const publicBuild = mode === "public";
  return {
    root: ".",
    base: publicBuild ? "./" : "/",
    publicDir: publicBuild ? "../../../data/gui/public" : "../../../data/gui/private",
    build: {
      outDir: publicBuild ? "dist-public" : "dist",
      rolldownOptions: {
        input: publicBuild ? "index.html" : { map: "index.html", operations: "ops.html" },
      },
    },
    test: {
      environment: "jsdom",
      include: ["tests/**/*.test.ts"],
    },
  };
});
