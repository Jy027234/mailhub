import { configDefaults, defineConfig } from "vitest/config";

/**
 * Semantics half of the accessibility gate.  It runs in jsdom and therefore
 * cannot see layout; the rendering half lives in `vitest.browser.config.ts`
 * (`*.browser.test.tsx`) and is excluded here so the two never double-run.
 */
export default defineConfig({
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
    exclude: [...configDefaults.exclude, "src/**/*.browser.test.tsx"],
  },
});
