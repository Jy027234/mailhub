#!/usr/bin/env node
/**
 * Locale gate for @fyjtech/mailhub-ui.
 *
 * The TypeScript types already force every built-in bundle to define every key.
 * This gate closes the two holes types cannot see:
 *
 *   1. no user-facing copy may stay hardcoded in a component (a translation that
 *      never happens looks like a working feature);
 *   2. every bundle must keep the placeholders its key declares, and both
 *      built-in bundles must expose exactly the same key set.
 *
 * Run via `npm run build` (after tsc) or `npm run check:locales`.
 */

import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const problems = [];

const { zhCN, enUS, messageKeys, validateMessages } = await import(
  new URL("../dist/i18n.js", import.meta.url).href
);

for (const [name, bundle] of [["zh-CN", zhCN], ["en-US", enUS]]) {
  for (const issue of validateMessages(bundle)) {
    problems.push(`${name}: ${issue}`);
  }
}

const zhKeys = messageKeys(zhCN).join(",");
const enKeys = messageKeys(enUS).join(",");
if (zhKeys !== enKeys) {
  problems.push("zh-CN and en-US bundles do not expose the same key set");
}

const CJK = /[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef]/;
for (const file of ["index.tsx", "standalone.tsx"]) {
  const source = await readFile(join(root, "src", file), "utf8");
  source.split(/\r?\n/).forEach((line, index) => {
    if (CJK.test(line)) {
      problems.push(
        `${file}:${index + 1}: hardcoded copy must move into src/i18n.ts`,
      );
    }
  });
}

if (problems.length > 0) {
  console.error("locale gate failed:");
  for (const problem of problems) {
    console.error(`  - ${problem}`);
  }
  process.exit(1);
}
console.log(`locale gate: ok (${messageKeys(zhCN).length} keys, zh-CN + en-US)`);
