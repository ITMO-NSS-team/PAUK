import { defineConfig } from "vitest/config";

export default defineConfig(({ mode }) => {
  const publicBuild = mode === "public";
  return {
    root: ".",
    base: publicBuild ? "./" : "/",
    publicDir: publicBuild ? "../../../data/gui/public" : "../../../data/gui/private",
    build: {
      outDir: publicBuild ? "dist-public" : "dist",
    },
    test: {
      environment: "jsdom",
      include: ["tests/**/*.test.ts"],
    },
  };
});
