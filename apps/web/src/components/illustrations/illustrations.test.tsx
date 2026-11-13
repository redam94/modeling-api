import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { CatalogGlyph, glyphKind, glyphVariants, type GlyphKind } from ".";

const KINDS: GlyphKind[] = ["model", "step", "plot", "workflow"];

/** A short description of an element for failure messages (tag and attributes). */
function describeEl(el: Element): string {
  return `<${el.tagName} ${Array.from(el.attributes)
    .map((a) => `${a.name}="${a.value}"`)
    .join(" ")}>`;
}

/** The element tree as plain data: tag, class, own text and children. */
function tree(el: Element): unknown {
  const text = Array.from(el.childNodes)
    .filter((n) => n.nodeType === Node.TEXT_NODE)
    .map((n) => n.textContent)
    .join("");
  return { tag: el.tagName.toLowerCase(), class: el.getAttribute("class"), text, children: Array.from(el.children).map(tree) };
}

/** Every drawn shape takes its colour from a token class: no colour attribute, no inline style. */
function expectTokenColoursOnly(svg: SVGElement) {
  for (const el of [svg, ...Array.from(svg.querySelectorAll("*"))]) {
    expect(el.getAttribute("style"), describeEl(el)).toBeNull();
    expect(el.getAttribute("fill"), describeEl(el)).toBeNull();
    expect(el.getAttribute("stroke"), describeEl(el)).toBeNull();
  }
  for (const shape of Array.from(svg.querySelectorAll("rect, circle, path"))) {
    expect(shape.getAttribute("class") ?? "", describeEl(shape)).toMatch(/(?:^|\s)fill-[a-z]/);
  }
  // Strokes are 2 px or more (§6.4).
  for (const el of Array.from(svg.querySelectorAll("[stroke-width]"))) {
    expect(Number(el.getAttribute("stroke-width")), describeEl(el)).toBeGreaterThanOrEqual(2);
  }
}

describe("illustrations", () => {
  it("keeps the source free of hex colours and HTML injection", () => {
    const dir = join(process.cwd(), "src/components/illustrations");
    const files = readdirSync(dir).filter((f) => f.endsWith(".tsx") && !f.endsWith(".test.tsx"));
    // the larger art is raster now (components/brand): what stays here is the glyphs
    expect(files.sort()).toEqual(["art.tsx", "catalog-glyph.tsx", "index.tsx"]);
    for (const f of files) {
      const text = readFileSync(join(dir, f), "utf8");
      expect([f, /#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(|oklch\(/.test(text)]).toEqual([f, false]);
      expect([f, /dangerouslySetInnerHTML|innerHTML|<image|<foreignObject|href=/.test(text)]).toEqual([f, false]);
    }
  });
});

describe("CatalogGlyph", () => {
  it.each(KINDS)("draws every %s variant in its own tile tint with token colours", (kind) => {
    for (let v = 0; v < glyphVariants(kind); v++) {
      const { container, unmount } = render(<CatalogGlyph kind={kind} variant={v} />);
      const tile = container.querySelector("[data-slot=catalog-glyph]")!;
      expect(tile).toHaveAttribute("data-kind", kind);
      const svg = tile.querySelector("svg")!;
      expect(svg).toHaveAttribute("aria-hidden", "true");
      expect(svg.childElementCount).toBeGreaterThan(0);
      expectTokenColoursOnly(svg);
      unmount();
    }
  });

  it("uses one tint per kind and a smaller tile for list rows", () => {
    const tints = KINDS.map((kind) => {
      const { container, unmount } = render(<CatalogGlyph kind={kind} size="sm" />);
      const tile = container.querySelector("[data-slot=catalog-glyph]")!;
      expect(tile).toHaveClass("size-11");
      const tint = Array.from(tile.classList).find((c) => c.startsWith("bg-"));
      unmount();
      return tint;
    });
    expect(tints).toEqual(["bg-accent", "bg-highlight-soft", "bg-info/10", "bg-muted"]);
  });

  it("picks the same drawing for the same seed", () => {
    const draw = (seed: string) => {
      const { container, unmount } = render(<CatalogGlyph kind="plot" seed={seed} />);
      const drawing = JSON.stringify(tree(container.querySelector("svg")!));
      unmount();
      return drawing;
    };
    expect(draw("core.fit_vs_actual")).toBe(draw("core.fit_vs_actual"));
    const seen = new Set(["core.fit_vs_actual", "core.ppc_fan", "core.roi_bars", "mmm.response_curves", "core.waterfall"].map(draw));
    expect(seen.size).toBeGreaterThan(1);
  });

  it("is labelled when asked and maps catalog and library kind words", () => {
    render(<CatalogGlyph kind="workflow" label="Workflow template" />);
    expect(screen.getByRole("img", { name: "Workflow template" })).toBeInTheDocument();
    expect(["model", "model_recipe", "prep_preset", "step", "plot", "recipe", "chart_template", "workflow_template", "node", "anything"].map(glyphKind)).toEqual([
      "model",
      "model",
      "step",
      "step",
      "plot",
      "plot",
      "plot",
      "workflow",
      "workflow",
      "model",
    ]);
  });
});
