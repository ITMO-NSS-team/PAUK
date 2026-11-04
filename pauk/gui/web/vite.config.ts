import { defineConfig } from "vitest/config";

// Data written by `pauk gui build`. The default mode serves private/ (full
// names, personal fields) for the lab server; `--mode public` builds the GitHub
// Pages site from public/ into dist-public/, so the two outputs never mix.
export default defineConfig(({ mode }) => {
  const isPublic = mode === "public";
  return {
    root: ".",
    // Pages serves the site from /<repo>/, so the public build uses relative URLs.
    base: isPublic ? "./" : "/",
    publicDir: isPublic ? "../../../data/gui/public" : "../../../data/gui/private",
    build: { outDir: isPublic ? "dist-public" : "dist", emptyOutDir: true },
    test: {
      environment: "jsdom",
      include: ["tests/**/*.test.ts"],
    },
  };
});
