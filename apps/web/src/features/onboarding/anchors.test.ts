/**
 * Tour anchors ⇔ source literals (v0.18 D18): a tour that points at an anchor no page
 * renders would sit on "This part isn't on screen right now" forever, and a `data-tour`
 * attribute no tour knows is a typo waiting to happen.
 *
 * An anchor counts as placed by a `data-tour="…"` literal, or by a quoted anchor name in a
 * file that sets `data-tour={…}` from a prop. Until WP-T7 (the anchor pass, last in the web
 * wave) has placed every anchor, the missing ones are listed in `PENDING`;
 * once T7 is in, `PENDING` becomes empty and the strict check is the normal one.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { anchorSelector, findAnchor, isTourAnchor, TOUR_ANCHORS, type TourAnchor } from "./anchors";
import { tourAnchors } from "./tours";

const SRC = resolve(__dirname, "../..");

function sourceFiles(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) {
      if (name === "node_modules" || name === "mocks" || name === "test") continue;
      sourceFiles(path, out);
    } else if (/\.(?:ts|tsx)$/.test(name) && !/\.test\.(?:ts|tsx)$/.test(name) && !name.endsWith(".d.ts")) {
      out.push(path);
    }
  }
  return out;
}

const files = sourceFiles(SRC).map((path) => ({ path: relative(SRC, path), text: readFileSync(path, "utf8") }));
const LITERAL = /data-tour="([^"]*)"/g;
const literals = new Map<string, string[]>();
const add = (name: string, path: string) => literals.set(name, [...(literals.get(name) ?? []), path]);
for (const f of files) {
  for (const match of f.text.matchAll(LITERAL)) add(match[1] ?? "", f.path);
  // A component that takes its anchor as a prop (`data-tour={anchor}`, default
  // `anchor = "home.playground"`) places every anchor it names as a quoted literal.
  if (/data-tour=\{/.test(f.text) && !f.path.endsWith("anchors.ts") && !f.path.endsWith("tours.ts")) {
    for (const a of TOUR_ANCHORS) if (f.text.includes(`"${a}"`)) add(a, f.path);
  }
}

/**
 * Anchors not placed yet when the tour engine landed (WP-T4). WP-T7 places them; the
 * integrator then empties this list. An anchor
 * not listed here must stay placed.
 */
const PENDING: ReadonlySet<TourAnchor> = new Set<TourAnchor>([]);

describe("tour anchors", () => {
  it("every anchor a tour points at is a listed anchor", () => {
    expect(tourAnchors().filter((a) => !isTourAnchor(a))).toEqual([]);
  });

  it("anchor names are plain (safe inside a CSS attribute selector) and unique", () => {
    for (const a of TOUR_ANCHORS) expect([a, /^[a-z][a-z0-9.-]*[a-z0-9]$/.test(a)]).toEqual([a, true]);
    expect(new Set(TOUR_ANCHORS).size).toBe(TOUR_ANCHORS.length);
    expect(anchorSelector("home.checklist")).toBe('[data-tour="home.checklist"]');
  });

  it("every data-tour literal in src/ names a listed anchor", () => {
    const unknown = [...literals.entries()].filter(([name]) => !isTourAnchor(name)).map(([name, where]) => `${name} (${where.join(", ")})`);
    expect(unknown).toEqual([]);
  });

  it("every listed anchor has a data-tour literal somewhere in src/ (or is still pending)", () => {
    const missing = TOUR_ANCHORS.filter((a) => !literals.has(a));
    expect(missing.filter((a) => !PENDING.has(a))).toEqual([]);
  });
});

describe("findAnchor", () => {
  it("skips a copy without a layout box (the hidden nav) and takes the shown one", () => {
    document.body.replaceChildren();
    const hidden = document.createElement("a");
    hidden.setAttribute("data-tour", "nav.catalog");
    const shown = document.createElement("a");
    shown.setAttribute("data-tour", "nav.catalog");
    shown.getBoundingClientRect = () => ({ top: 1, left: 1, width: 20, height: 10, right: 21, bottom: 11, x: 1, y: 1, toJSON: () => ({}) });
    document.body.append(hidden, shown);
    expect(findAnchor("nav.catalog")).toBe(shown);
    shown.remove();
    expect(findAnchor("nav.catalog")).toBeNull();
  });
});
