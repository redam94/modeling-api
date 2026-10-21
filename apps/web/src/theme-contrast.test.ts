/**
 * WCAG 2.1 AA contrast for the theme tokens in app.css, in both themes:
 * - status text (Badge `text-xs`, banners) needs 4.5:1, on its tinted badge background as well
 *   as on the card and the page background;
 * - form field borders (`--input`), the focus ring and chart series are non-text UI and need 3:1
 *   against what they sit on;
 * - every foreground / background pair the primitives use (primary buttons, accent, tooltips,
 *   destructive fills, hint text) needs 4.5:1.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const css = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "app.css"), "utf8");

function tokens(selector: string): Record<string, string> {
  const start = css.indexOf(`${selector} {`);
  if (start < 0) throw new Error(`no ${selector} block`);
  const body = css.slice(start, css.indexOf("}", start));
  return Object.fromEntries([...body.matchAll(/--([\w-]+):\s*(#[0-9a-fA-F]{6})\s*;/g)].map((m) => [m[1]!, m[2]!.toLowerCase()]));
}

type Rgb = [number, number, number];
const rgb = (hex: string): Rgb => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16)) as Rgb;
const mix = (fg: Rgb, bg: Rgb, alpha: number): Rgb => fg.map((c, i) => c * alpha + bg[i]! * (1 - alpha)) as Rgb;
function luminance([r, g, b]: Rgb): number {
  const lin = (c: number) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}
function contrast(a: Rgb, b: Rgb): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x) as [number, number];
  return (hi + 0.05) / (lo + 0.05);
}

/** Badge tint alpha per status token (badgeVariants in components/ui/primitives.tsx). */
const STATUS_BADGES: [token: string, alpha: number][] = [
  ["success", 0.12],
  ["warning", 0.14],
  ["info", 0.12],
  ["destructive", 0.12],
];

describe.each([
  ["light", ":root"],
  ["dark", ".dark"],
])("%s theme contrast", (_name, selector) => {
  const t = tokens(selector);
  const surfaces: [string, Rgb][] = [
    ["card", rgb(t.card!)],
    ["background", rgb(t.background!)],
    ["popover", rgb(t.popover!)],
  ];
  const on = (fg: string, bg: string) => contrast(rgb(t[fg]!), rgb(t[bg]!));

  it("parses the whole block (a format slip fails loudly)", () => {
    expect(Object.keys(t).length).toBeGreaterThanOrEqual(40);
  });

  it.each(STATUS_BADGES)("--%s text reads at 4.5:1 on its badge, the card and the page", (token, alpha) => {
    const fg = rgb(t[token]!);
    for (const [name, surface] of surfaces) {
      const badge = mix(fg, surface, alpha);
      expect(contrast(fg, badge), `${token} on its badge over ${name}`).toBeGreaterThanOrEqual(4.5);
      expect(contrast(fg, surface), `${token} on ${name}`).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("--muted-foreground reads at 4.5:1 on the card and the page", () => {
    for (const [name, surface] of surfaces) {
      expect(contrast(rgb(t["muted-foreground"]!), surface), name).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("--input borders reach 3:1 against the card, the page and muted", () => {
    for (const [name, surface] of surfaces) {
      expect(contrast(rgb(t.input!), surface), name).toBeGreaterThanOrEqual(3);
    }
    expect(on("input", "muted"), "muted").toBeGreaterThanOrEqual(3);
  });

  it("--primary reads as link text at 4.5:1 on the card and the page", () => {
    expect(on("primary", "card")).toBeGreaterThanOrEqual(4.5);
    expect(on("primary", "background")).toBeGreaterThanOrEqual(4.5);
  });

  it("--hint reads at 4.5:1 on the card, the page and muted", () => {
    for (const bg of ["card", "background", "muted"]) {
      expect(on("hint", bg), bg).toBeGreaterThanOrEqual(4.5);
    }
  });

  it.each([
    ["primary-foreground", "primary"],
    ["accent-foreground", "accent"],
    ["tooltip-foreground", "tooltip"],
    ["destructive-foreground", "destructive"],
  ])("--%s reads at 4.5:1 on --%s", (fg, bg) => {
    expect(on(fg, bg)).toBeGreaterThanOrEqual(4.5);
  });

  it("--ring reaches 3:1 against the card and the page", () => {
    expect(on("ring", "card")).toBeGreaterThanOrEqual(3);
    expect(on("ring", "background")).toBeGreaterThanOrEqual(3);
  });

  it.each(["series-1", "series-2", "series-3", "series-4", "series-5", "series-6", "series-7", "series-8", "chart-index"])(
    "--%s reaches 3:1 against the card",
    (token) => {
      expect(on(token, "card")).toBeGreaterThanOrEqual(3);
    },
  );
});
