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
  ...["100", "200", "300", "400", "500", "600", "700", "800"].map((s) => `brand-${s}`),
  ...["000", "100", "200", "300", "400", "500", "600", "700", "800", "900", "1000"].map((s) => `grey-${s}`),
];

/** semantic token -> ramp token, per theme. */
const SOURCES: Record<"light" | "dark", Record<string, string>> = {
  light: {
    background: "grey-100",
    foreground: "grey-1000",
    card: "grey-000",
    "card-foreground": "grey-1000",
    popover: "grey-000",
    "popover-foreground": "grey-1000",
    primary: "brand-500",
    "primary-foreground": "grey-000",
    "primary-hover": "brand-400",
    "primary-active": "brand-600",
    secondary: "grey-200",
    "secondary-foreground": "grey-1000",
    muted: "grey-200",
    "muted-foreground": "grey-800",
    hint: "grey-700",
    accent: "brand-100",
    "accent-foreground": "brand-600",
    info: "brand-600",
    border: "grey-300",
    "border-strong": "grey-400",
    sidebar: "grey-000",
    "surface-hover": "grey-200",
    tooltip: "grey-900",
    "tooltip-foreground": "grey-000",
    overlay: "grey-800",
    "chart-index": "brand-500",
    "chart-band-50": "brand-300",
    "chart-grid": "grey-300",
    "chart-base": "grey-400",
    "chart-ref": "grey-700",
    "chart-net": "grey-900",
    "chart-plain": "grey-600",
    "zone-headroom-tint": "brand-100",
    "zone-nopay-tint": "grey-200",
  },
  dark: {
    background: "grey-1000",
    foreground: "grey-200",
    "card-foreground": "grey-200",
    "popover-foreground": "grey-200",
    "secondary-foreground": "grey-200",
    primary: "brand-300",
    "primary-foreground": "brand-800",
    "primary-active": "brand-200",
    "muted-foreground": "grey-500",
    "accent-foreground": "brand-200",
    ring: "brand-300",
    tooltip: "grey-300",
    "tooltip-foreground": "grey-1000",
    "chart-index": "brand-300",
    "chart-ref": "grey-500",
    "chart-net": "grey-300",
  },
};

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
});

it("the raw ramps are the same in both themes", () => {
  const light = tokens(":root");
  const dark = tokens(".dark");
  for (const ramp of RAMPS) expect(dark[ramp], ramp).toBe(light[ramp]);
});

it("every block token is exposed to Tailwind as --color-*", () => {
  const theme = css.slice(css.indexOf("@theme inline {"));
  for (const token of Object.keys(tokens(":root"))) {
    expect(theme, token).toContain(`--color-${token}: var(--${token});`);
  }
});
