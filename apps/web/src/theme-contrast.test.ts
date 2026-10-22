/**
 * WCAG 2.1 AA contrast for the theme tokens in app.css, in both themes:
 * - status text (Badge `text-xs`, banners) needs 4.5:1, on its tinted badge background as well
 *   as on the card and the page background;
 * - form field borders (`--input`), the focus ring and chart series are non-text UI and need 3:1
 *   against what they sit on;
 * - every foreground / background pair the primitives use (primary buttons, accent, tooltips,
 *   destructive fills, hint text) needs 4.5:1;
 * - the Studio roles: text on the ink block, the lime highlighter and the coral tints needs 4.5:1,
 *   the highlighter as a mark on the block and the coral fill on a card need 3:1.
 * The lime `--highlight` is a fill only (about 1.2:1 on a card): a lint below refuses it as text.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative } from "node:path";
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

  it.each([
    ["foreground", "card"],
    ["foreground", "background"],
    ["secondary-foreground", "secondary"],
    // A pressed filter pill (bg-foreground text-background).
    ["background", "foreground"],
    ["muted-foreground", "muted"],
  ])("--%s reads at 4.5:1 on --%s", (fg, bg) => {
    expect(on(fg, bg)).toBeGreaterThanOrEqual(4.5);
  });

  it.each([
    // The call to action on a block, the "up" delta pill, the active tour dot.
    ["highlight-foreground", "highlight"],
    // Lime words: the current step, "new" chips.
    ["highlight-ink", "highlight-soft"],
    ["highlight-ink", "card"],
    ["highlight-ink", "background"],
    ["muted-foreground", "highlight-soft"],
    // The one ink block per page and the tiles inside it.
    ["block-foreground", "block"],
    ["block-foreground", "block-raised"],
    ["block-muted", "block"],
    ["block-muted", "block-raised"],
    // Coral words: down deltas, "needs a look" chips.
    ["attention", "attention-soft"],
    ["attention", "card"],
    ["attention", "background"],
  ])("role text --%s reads at 4.5:1 on --%s", (fg, bg) => {
    expect(on(fg, bg)).toBeGreaterThanOrEqual(4.5);
  });

  it.each([
    // The active nav icon, the coach-mark's active dot and the CTA edge on a block.
    ["highlight", "block"],
    ["highlight", "block-raised"],
    // The response curve and accent dots.
    ["attention-fill", "card"],
    ["attention-fill", "background"],
  ])("role mark --%s reaches 3:1 on --%s", (fg, bg) => {
    expect(on(fg, bg)).toBeGreaterThanOrEqual(3);
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

describe("the keyboard highlight of menus and the command palette", () => {
  const src = (rel: string) => readFileSync(join(dirname(fileURLToPath(import.meta.url)), rel), "utf8");

  it("uses the accent tint with a ring-coloured bar, never the near-invisible surface-hover", () => {
    const overlays = src("components/ui/overlays.tsx");
    const palette = src("components/shell/command-palette.tsx");
    expect(overlays).not.toMatch(/data-\[highlighted\]:bg-surface-hover/);
    expect(overlays.match(/data-\[highlighted\]:shadow-\[inset_3px_0_0_var\(--ring\)\]/g)?.length).toBeGreaterThanOrEqual(2);
    expect(palette).toContain('i === active && "bg-accent text-accent-foreground shadow-[inset_3px_0_0_var(--ring)]"');
  });

  it.each([
    ["light", ":root"],
    ["dark", ".dark"],
  ])("the bar has 3:1 on the popover and the text 4.5:1 on the tint (%s)", (_n, selector) => {
    const t = tokens(selector);
    expect(contrast(rgb(t.ring!), rgb(t.popover!))).toBeGreaterThanOrEqual(3);
    expect(contrast(rgb(t.ring!), rgb(t.accent!))).toBeGreaterThanOrEqual(3);
    expect(contrast(rgb(t["accent-foreground"]!), rgb(t.accent!))).toBeGreaterThanOrEqual(4.5);
  });
});

describe("the lime highlighter is never a text colour", () => {
  const root = dirname(fileURLToPath(import.meta.url));
  const self = fileURLToPath(import.meta.url);
  const files = (dir: string): string[] =>
    readdirSync(dir).flatMap((name) => {
      const path = join(dir, name);
      return statSync(path).isDirectory() ? files(path) : /\.(tsx?|css)$/.test(name) ? [path] : [];
    });
  // `text-highlight` alone (any variant, any opacity) and `text-highlight-soft` are lime text;
  // `text-highlight-foreground` (ink on a lime fill) and `text-highlight-ink` are the text roles.
  // Icons on a block take `stroke-highlight` (lucide strokes in currentColor otherwise).
  const misuse = [
    /(?<![\w-])text-highlight(?!-(?:foreground|ink)(?![\w-]))(?!\w)/,
    /text-\[(?:color:)?var\(--highlight\)\]/,
    /(?<![\w-])color:\s*var\(--highlight\)/,
  ];

  it("no source file colours text with --highlight", () => {
    const offenders: string[] = [];
    for (const path of files(root)) {
      if (path === self) continue;
      const text = readFileSync(path, "utf8");
      text.split("\n").forEach((line, i) => {
        if (misuse.some((re) => re.test(line))) offenders.push(`${relative(root, path)}:${i + 1}: ${line.trim()}`);
      });
    }
    expect(offenders, "use text-highlight-ink for lime words, text-highlight-foreground on a lime fill").toEqual([]);
  });

  it("the lint catches the misuse and lets the text roles through", () => {
    const hits = (line: string) => misuse.some((re) => re.test(line));
    for (const bad of ['"text-highlight"', "hover:text-highlight", "text-highlight/80", "text-highlight-soft", "color: var(--highlight);"]) {
      expect(hits(bad), bad).toBe(true);
    }
    for (const good of ["text-highlight-foreground", "text-highlight-ink", "bg-highlight", "stroke-highlight", "--highlight-ink: #4c6600;"]) {
      expect(hits(good), good).toBe(false);
    }
  });
});
