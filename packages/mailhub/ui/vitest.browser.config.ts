import { playwright } from "@vitest/browser-playwright";
import { defineConfig } from "vitest/config";

/**
 * Rendering half of the WCAG 2.2 AA gate.
 *
 * jsdom has no layout engine, so contrast, target size, reflow and motion can
 * only be proved in a real browser.  This is a separate config (and a separate
 * release gate) so the default `npm test` stays fast and hermetic while the
 * browser pass still runs headlessly in CI via Playwright Chromium.
 */
export default defineConfig({
  test: {
    include: ["src/**/*.browser.test.tsx"],
    browser: {
      enabled: true,
      headless: true,
      // The audit measures the contract at a desktop baseline and then shrinks
      // the iframe itself for the 1.4.10 / 1.4.4 cases.
      viewport: { width: 1280, height: 900 },
      instances: [{ browser: "chromium", provider: playwright() }],
    },
  },
});
