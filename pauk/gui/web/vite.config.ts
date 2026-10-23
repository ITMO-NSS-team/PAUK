import { loadEnv } from "vite";
import { defineConfig } from "vitest/config";

export default defineConfig(({ mode }) => {
  // The university whose data is built in (pauk/institution.py); deploy.sh
  // passes it as VITE_INSTITUTION, and src/core/i18n.ts names it in the UI.
  const institution = loadEnv(mode, ".", "VITE_").VITE_INSTITUTION || "itmo";
  return {
    root: ".",
    // Data written by `pauk gui build`. The full private/ variant is served;
    // public/ (no personal fields) is not wired up yet.
    publicDir: `../../../data/gui/${institution}/private`,
    test: {
      environment: "jsdom",
      include: ["tests/**/*.test.ts"],
    },
  };
});
