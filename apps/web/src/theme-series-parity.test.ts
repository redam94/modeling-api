/**
 * The plot sandbox draws charts in its own origin, so it cannot read app.css: its `SERIES`
 * table (apps/plot-sandbox/src/theme.ts) repeats the `--series-*` tokens and its `INK` table the
 * text, grid and axis tokens. This keeps them equal in both themes, so a plot and the chart beside
 * it colour the same entity alike and sit on the same inks.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const css = readFileSync(join(here, "app.css"), "utf8");
const sandbox = readFileSync(join(here, "..", "..", "plot-sandbox", "src", "theme.ts"), "utf8");

function series(selector: string): string[] {
  const start = css.indexOf(`${selector} {`);
  if (start < 0) throw new Error(`no ${selector} block`);
  const body = css.slice(start, css.indexOf("}", start));
  const found = [...body.matchAll(/--series-(\d+):\s*(#[0-9a-fA-F]{6})\s*;/g)];
  return found.sort((a, b) => Number(a[1]) - Number(b[1])).map((m) => m[2]!.toLowerCase());
}

function token(selector: string, name: string): string {
  const start = css.indexOf(`${selector} {`);
  const body = css.slice(start, css.indexOf("}", start));
  const found = new RegExp(`--${name}:\\s*(#[0-9a-fA-F]{6})\\s*;`).exec(body);
  if (!found) throw new Error(`no --${name} in ${selector}`);
  return found[1]!.toLowerCase();
}

function sandboxInk(theme: "light" | "dark"): Record<string, string> {
  const table = sandbox.slice(sandbox.indexOf("export const INK"));
  const row = new RegExp(`${theme}:\\s*\\{([^}]*)\\}`).exec(table);
  if (!row) throw new Error(`no ${theme} row in the sandbox INK`);
  return Object.fromEntries([...row[1]!.matchAll(/(\w+):\s*"(#[0-9a-fA-F]{6})"/g)].map((m) => [m[1]!, m[2]!.toLowerCase()]));
}

/** sandbox INK key -> app.css token. */
const INK_TOKENS = { text: "foreground", muted: "muted-foreground", grid: "chart-grid", domain: "chart-base" };

function sandboxSeries(theme: "light" | "dark"): string[] {
  const table = sandbox.slice(sandbox.indexOf("export const SERIES"));
  const row = new RegExp(`${theme}:\\s*\\[([^\\]]*)\\]`).exec(table);
  if (!row) throw new Error(`no ${theme} row in the sandbox SERIES`);
  return [...row[1]!.matchAll(/"(#[0-9a-fA-F]{6})"/g)].map((m) => m[1]!.toLowerCase());
}

describe.each([
  ["light", ":root"],
  ["dark", ".dark"],
] as const)("%s series", (theme, selector) => {
  it("equals the plot sandbox SERIES, in order", () => {
    const tokens = series(selector);
    expect(tokens).toHaveLength(8);
    expect(sandboxSeries(theme)).toEqual(tokens);
  });

  it("the plot sandbox INK equals the text, grid and axis tokens", () => {
    const ink = sandboxInk(theme);
    for (const [key, name] of Object.entries(INK_TOKENS)) expect(ink[key], key).toBe(token(selector, name));
  });
});
