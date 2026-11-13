/**
 * The semantic tokens in app.css repeat their ramp value as hex (the contrast test parses hex
 * only), so nothing ties them together but this test: each documented semantic token must equal
 * its ramp source, and both blocks must carry the same raw ramps.
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

const RAMPS = [
  // The reference greys, white to black. 550 and 650 are the in-between steps contrast needs:
  // field borders (3:1 on muted) and secondary text (4.5:1 on muted).
  ...["000", "050", "100", "200", "300", "400", "500", "550", "600", "650", "700", "800", "900", "950", "1000"].map((s) => `grey-${s}`),
  // The four data hues: 100-300 tints, 300/400 the dark-theme hue, 500 the light-theme hue, 600
  // the light-theme text ink, 800-950 the dark-theme fills.
  ...["100", "200", "300", "400", "500", "600", "800", "900", "950"].map((s) => `blue-${s}`),
  ...["100", "300", "500", "600", "950"].map((s) => `orange-${s}`),
  ...["100", "200", "300", "500", "600", "800", "950"].map((s) => `green-${s}`),
  ...["100", "200", "300", "500", "600", "800", "950"].map((s) => `red-${s}`),
];

/** semantic token -> ramp token, per theme. Chrome sits on the grey ramp in both themes; colour
 * comes from the four data ramps only. */
const SOURCES: Record<"light" | "dark", Record<string, string>> = {
  light: {
    background: "grey-100",
    foreground: "grey-900",
    card: "grey-000",
    "card-foreground": "grey-900",
    popover: "grey-000",
    "popover-foreground": "grey-900",
    primary: "grey-900",
    "primary-foreground": "grey-000",
    "primary-hover": "grey-700",
    "primary-active": "grey-1000",
    secondary: "grey-200",
    "secondary-foreground": "grey-900",
    muted: "grey-200",
    "muted-foreground": "grey-650",
    hint: "grey-650",
    accent: "grey-200",
    "accent-foreground": "grey-900",
    destructive: "red-600",
    "destructive-foreground": "grey-000",
    success: "green-600",
    warning: "orange-600",
    info: "blue-600",
    border: "grey-200",
    "border-strong": "grey-300",
    input: "grey-550",
    ring: "grey-900",
    sidebar: "grey-100",
    "surface-hover": "grey-200",
    tooltip: "grey-900",
    "tooltip-foreground": "grey-000",
    overlay: "grey-700",
    "series-1": "blue-500",
    "series-2": "orange-500",
    "series-3": "green-500",
    "series-4": "red-500",
    "series-8": "grey-600",
    "chart-index": "blue-500",
    "chart-band": "blue-200",
    "chart-band-50": "blue-300",
    "chart-grid": "grey-200",
    "chart-base": "grey-300",
    "chart-ref": "grey-650",
    "chart-gain": "green-500",
    "chart-loss": "red-500",
    "chart-net": "grey-700",
    "chart-now": "orange-500",
    "chart-plain": "grey-500",
    "zone-headroom": "blue-200",
    "zone-headroom-tint": "blue-100",
    "zone-optimal": "green-200",
    "zone-optimal-tint": "green-100",
    "zone-over": "red-200",
    "zone-over-tint": "red-100",
    "zone-nopay": "grey-300",
    "zone-nopay-tint": "grey-200",
    highlight: "orange-300",
    "highlight-foreground": "grey-900",
    "highlight-soft": "orange-100",
    "highlight-ink": "orange-600",
    block: "grey-900",
    "block-foreground": "grey-100",
    "block-muted": "grey-400",
    "block-raised": "grey-800",
    attention: "red-600",
    "attention-soft": "red-100",
    "attention-fill": "red-500",
    "illus-1": "grey-700",
    "illus-2": "orange-300",
    "illus-3": "blue-500",
    "illus-4": "grey-500",
  },
  dark: {
    background: "grey-1000",
    foreground: "grey-100",
    card: "grey-950",
    "card-foreground": "grey-100",
    popover: "grey-900",
    "popover-foreground": "grey-100",
    primary: "grey-100",
    "primary-foreground": "grey-900",
    "primary-hover": "grey-300",
    "primary-active": "grey-000",
    secondary: "grey-800",
    "secondary-foreground": "grey-100",
    muted: "grey-800",
    "muted-foreground": "grey-400",
    hint: "grey-400",
    accent: "grey-800",
    "accent-foreground": "grey-100",
    destructive: "red-300",
    "destructive-foreground": "grey-900",
    success: "green-300",
    warning: "orange-300",
    info: "blue-400",
    border: "grey-800",
    "border-strong": "grey-700",
    input: "grey-500",
    ring: "grey-100",
    sidebar: "grey-1000",
    "surface-hover": "grey-800",
    tooltip: "grey-100",
    "tooltip-foreground": "grey-900",
    overlay: "grey-1000",
    "series-1": "blue-400",
    "series-2": "orange-300",
    "series-3": "green-300",
    "series-4": "red-300",
    "chart-index": "blue-400",
    "chart-band": "blue-900",
    "chart-band-50": "blue-800",
    "chart-grid": "grey-800",
    "chart-base": "grey-700",
    "chart-ref": "grey-400",
    "chart-gain": "green-300",
    "chart-loss": "red-300",
    "chart-net": "grey-100",
    "chart-now": "orange-300",
    "chart-plain": "grey-500",
    "zone-headroom": "blue-900",
    "zone-headroom-tint": "blue-950",
    "zone-optimal": "green-800",
    "zone-optimal-tint": "green-950",
    "zone-over": "red-800",
    "zone-over-tint": "red-950",
    "zone-nopay": "grey-700",
    "zone-nopay-tint": "grey-800",
    highlight: "orange-300",
    "highlight-foreground": "grey-900",
    "highlight-soft": "orange-950",
    "highlight-ink": "orange-300",
    block: "grey-900",
    "block-foreground": "grey-100",
    "block-muted": "grey-400",
    "block-raised": "grey-800",
    attention: "red-300",
    "attention-soft": "red-950",
    "attention-fill": "red-300",
    "illus-1": "grey-300",
    "illus-2": "orange-300",
    "illus-3": "blue-400",
    "illus-4": "grey-500",
  },
};

/** Derived tokens that repeat another semantic token in both themes. */
const SAME_AS: [token: string, source: string][] = [
  ["card-foreground", "foreground"],
  ["popover-foreground", "foreground"],
  ["secondary-foreground", "foreground"],
  ["sidebar", "background"],
  ["surface-hover", "secondary"],
  // Chrome is monochrome: the primary action and the focus ring are the ink.
  ["primary", "foreground"],
  ["ring", "foreground"],
  ["zone-headroom", "chart-band"],
  ["zone-over-tint", "attention-soft"],
  ["zone-nopay", "border-strong"],
  ["zone-nopay-tint", "secondary"],
  ["illus-2", "highlight"],
  ["illus-3", "chart-index"],
];

describe.each([
  ["light", ":root"],
  ["dark", ".dark"],
] as const)("%s theme tokens", (name, selector) => {
  const t = tokens(selector);

  it("carries every raw ramp step", () => {
    for (const ramp of RAMPS) expect(t[ramp], ramp).toMatch(/^#[0-9a-f]{6}$/);
  });

  it.each(Object.entries(SOURCES[name]))("--%s equals --%s", (semantic, ramp) => {
    expect(t[semantic]).toBe(t[ramp]);
  });

  it.each(SAME_AS)("--%s repeats --%s", (token, source) => {
    expect(t[token]).toMatch(/^#[0-9a-f]{6}$/);
    expect(t[token]).toBe(t[source]);
  });
});

it("the raw ramps are the same in both themes", () => {
  const light = tokens(":root");
  const dark = tokens(".dark");
  for (const ramp of RAMPS) expect(dark[ramp], ramp).toBe(light[ramp]);
});

it("both themes define the same tokens", () => {
  expect(Object.keys(tokens(".dark")).sort()).toEqual(Object.keys(tokens(":root")).sort());
});

it("keeps the role tokens every surface codes against", () => {
  const roles = ["highlight", "highlight-foreground", "highlight-soft", "highlight-ink", "block", "block-foreground"];
  roles.push("block-muted", "block-raised", "attention", "attention-soft", "attention-fill");
  roles.push("illus-1", "illus-2", "illus-3", "illus-4");
  for (const selector of [":root", ".dark"]) {
    const t = tokens(selector);
    for (const role of roles) expect(t[role], `${selector} --${role}`).toMatch(/^#[0-9a-f]{6}$/);
  }
});

it("defines the display face, the display sizes and the 3xl radius", () => {
  const theme = css.slice(css.indexOf("@theme inline {"));
  expect(theme).toContain('--font-display: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Inter Variable"');
  expect(css).not.toContain("Bricolage");
  for (const size of ["3xl", "4xl", "5xl"]) expect(theme).toMatch(new RegExp(`--text-${size}: [\\d.]+rem;`));
  expect(theme).toContain("--radius-3xl: 28px;");
});

it("every block token is exposed to Tailwind as --color-*", () => {
  const theme = css.slice(css.indexOf("@theme inline {"));
  for (const token of Object.keys(tokens(":root"))) {
    expect(theme, token).toContain(`--color-${token}: var(--${token});`);
  }
});
