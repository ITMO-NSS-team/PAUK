import { defineConfig } from "vitest/config";

export default defineConfig({
  root: ".",
  // Data written by `pauk gui build`. The full private/ variant is served;
  // public/ (no personal fields) is not wired up yet.
  publicDir: "../../../data/gui/private",
  test: {
    environment: "jsdom",
    include: ["tests/**/*.test.ts"],
  },
});
