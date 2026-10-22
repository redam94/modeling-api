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
  ...["100", "200", "300", "400", "500", "600", "700", "800", "900"].map((s) => `brand-${s}`),
  ...["000", "100", "200", "300", "400", "500", "600", "700", "800", "900", "1000"].map((s) => `grey-${s}`),
  // The Studio highlighter and attention ramps: only the steps the direction defines.
  ...["100", "200", "300", "400", "600", "700"].map((s) => `lime-${s}`),
  ...["100", "300", "500", "700"].map((s) => `coral-${s}`),
];

/** semantic token -> ramp token, per theme. The Studio dark canvas is off the grey ramp, so its
 * page, ink and chart-ink tokens are literals and only the brand, lime and coral rows hold. */
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
    "destructive-foreground": "grey-000",
    info: "brand-600",
    border: "grey-300",
    "border-strong": "grey-400",
    input: "grey-600",
    ring: "brand-500",
    sidebar: "grey-100",
    "surface-hover": "grey-200",
    tooltip: "brand-800",
    "tooltip-foreground": "grey-000",
    overlay: "grey-800",
    "series-1": "brand-500",
    "chart-index": "brand-500",
    "chart-band": "brand-200",
    "chart-band-50": "brand-300",
    "chart-grid": "grey-300",
    "chart-base": "grey-400",
    "chart-ref": "grey-700",
    "chart-loss": "coral-500",
    "chart-net": "grey-900",
    "chart-now": "coral-500",
    "chart-plain": "grey-600",
    "zone-headroom": "brand-200",
    "zone-headroom-tint": "brand-100",
    "zone-optimal": "lime-300",
    "zone-optimal-tint": "lime-100",
    "zone-over-tint": "coral-100",
    "zone-nopay": "grey-400",
    "zone-nopay-tint": "grey-200",
    highlight: "lime-300",
    "highlight-foreground": "brand-800",
    "highlight-soft": "lime-100",
    "highlight-ink": "lime-600",
    block: "brand-800",
    "block-foreground": "grey-000",
    attention: "coral-700",
    "attention-soft": "coral-100",
    "attention-fill": "coral-500",
    "illus-1": "brand-400",
    "illus-2": "brand-300",
    "illus-3": "lime-300",
    "illus-4": "coral-500",
  },
  dark: {
    primary: "brand-300",
    "primary-foreground": "brand-800",
    "primary-active": "brand-200",
    "accent-foreground": "brand-200",
    ring: "brand-300",
    "tooltip-foreground": "brand-800",
    "series-1": "brand-300",
    "chart-index": "brand-300",
    "zone-optimal": "lime-600",
    highlight: "lime-300",
    "highlight-foreground": "brand-800",
    "highlight-ink": "lime-300",
    "block-foreground": "grey-000",
    attention: "coral-300",
    "illus-1": "brand-400",
    "illus-2": "brand-300",
    "illus-3": "lime-300",
  },
};

/** Derived tokens that repeat another semantic token in both themes. */
const SAME_AS: [token: string, source: string][] = [
  ["card-foreground", "foreground"],
  ["popover-foreground", "foreground"],
  ["secondary-foreground", "foreground"],
  ["sidebar", "background"],
  ["surface-hover", "secondary"],
  ["zone-headroom", "chart-band"],
  ["zone-headroom-tint", "accent"],
  ["zone-nopay", "border-strong"],
  ["zone-nopay-tint", "secondary"],
  ["illus-3", "highlight"],
  ["illus-4", "attention-fill"],
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

it("keeps the role tokens every v0.18 surface codes against", () => {
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
  expect(theme).toContain('--font-display: "Bricolage Grotesque Variable"');
  for (const size of ["3xl", "4xl", "5xl"]) expect(theme).toMatch(new RegExp(`--text-${size}: [\\d.]+rem;`));
  expect(theme).toContain("--radius-3xl: 28px;");
});

it("every block token is exposed to Tailwind as --color-*", () => {
  const theme = css.slice(css.indexOf("@theme inline {"));
  for (const token of Object.keys(tokens(":root"))) {
    expect(theme, token).toContain(`--color-${token}: var(--${token});`);
  }
});
