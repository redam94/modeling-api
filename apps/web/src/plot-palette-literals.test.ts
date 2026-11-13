/**
 * Plots the app builds itself (governance evidence, the science explorer) render in the plot
 * sandbox, which swaps only the recipe palette's colours for their dark variants
 * (`apps/plot-sandbox/src/recipe-palette.ts`). A literal colour outside that palette keeps its
 * light value on the dark card (a literal magenta was 2.6:1 there), so these specs use palette colours
 * only, apart from a neutral grey fill that reads on both cards.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { DARK_VARIANTS } from "../../plot-sandbox/src/recipe-palette";

const here = dirname(fileURLToPath(import.meta.url));
const NEUTRALS = new Set(["#d2d2d7"]); // grey-300: the "No value" fill of a map

describe.each([
  "features/governance/evidence.tsx",
  "features/explore/science-plots.ts",
  "features/score/compare.ts",
  "features/explore/plots.ts",
])("%s", (rel) => {
  it("draws with recipe-palette colours (swapped in dark mode) or the neutral grey only", () => {
    const literals = [...readFileSync(join(here, rel), "utf8").matchAll(/"(#[0-9a-fA-F]{6})"/g)].map((m) => m[1]!.toLowerCase());
    const off = literals.filter((c) => !(c in DARK_VARIANTS) && !NEUTRALS.has(c));
    expect(off).toEqual([]);
  });
});
