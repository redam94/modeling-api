import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { EmptyState } from "@/components/common";

import {
  CatalogGlyph,
  EmptyDashboardsArt,
  EmptyDatasetsArt,
  EmptyLibraryArt,
  EmptyModelsArt,
  EmptyRunsArt,
  EmptySearchArt,
  EmptyWorkflowsArt,
  glyphKind,
  glyphVariants,
  HeroArt,
  type GlyphKind,
} from ".";

const ART = { EmptyDashboardsArt, EmptyDatasetsArt, EmptyLibraryArt, EmptyModelsArt, EmptyRunsArt, EmptySearchArt, EmptyWorkflowsArt, HeroArt };
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
  it.each(Object.entries(ART))("%s is decorative by default and has no text, images or foreign content", (_name, Component) => {
    const { container } = render(<Component />);
    const svg = container.querySelector("svg")!;
    expect(svg).toHaveAttribute("aria-hidden", "true");
    expect(svg).not.toHaveAttribute("role");
    expect(svg.querySelector("text, image, foreignObject, script, use, a")).toBeNull();
    expect(svg.textContent).toBe("");
    expectTokenColoursOnly(svg);
  });

  it.each(Object.entries(ART))("%s becomes one labelled image with a label", (_name, Component) => {
    render(<Component label="A dashboard being laid out" />);
    const img = screen.getByRole("img", { name: "A dashboard being laid out" });
    expect(img).not.toHaveAttribute("aria-hidden");
  });

  it("assembles the hero once on request and draws it still otherwise", () => {
    const { container, rerender } = render(<HeroArt />);
    expect(container.querySelectorAll(".assemble-bar")).toHaveLength(4);
    expect(container.querySelectorAll(".assemble-draw")).toHaveLength(1);
    expect(container.querySelectorAll(".assemble-sun")).toHaveLength(1);
    // The stagger counts rects with nth-of-type: the four bars are the first four rects.
    expect(Array.from(container.querySelectorAll("rect")).slice(0, 4).every((r) => r.classList.contains("assemble-bar"))).toBe(true);
    rerender(<HeroArt animate={false} />);
    expect(container.querySelector("[class*=assemble]")).toBeNull();
  });

  it("keeps the source free of hex colours and HTML injection", () => {
    const dir = join(process.cwd(), "src/components/illustrations");
    const files = readdirSync(dir).filter((f) => f.endsWith(".tsx") && !f.endsWith(".test.tsx"));
    expect(files.length).toBeGreaterThanOrEqual(10);
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
    expect(tints).toEqual(["bg-accent", "bg-highlight-soft", "bg-attention-soft", "bg-muted"]);
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

describe("EmptyState slots", () => {
  it("keeps the plain text-only card when no slot is used", () => {
    const { container } = render(
      <EmptyState title="No runs yet" action={<button type="button">Start</button>}>
        Fit a model to see it here.
      </EmptyState>,
    );
    expect(tree(container.firstElementChild!)).toEqual({
      tag: "div",
      class: "flex flex-col items-start gap-2 rounded-xl border border-dashed border-border-strong bg-card p-8",
      text: "",
      children: [
        { tag: "h3", class: "font-semibold", text: "No runs yet", children: [] },
        { tag: "div", class: "max-w-[60ch] text-muted-foreground", text: "Fit a model to see it here.", children: [] },
        { tag: "div", class: "pt-2", text: "", children: [{ tag: "button", class: null, text: "Start", children: [] }] },
      ],
    });
  });

  it("puts the primary action first and shows the art beside the words", () => {
    render(
      <EmptyState
        title="Your first dashboard starts from a finished run."
        illustration={<EmptyDashboardsArt />}
        primaryAction={<button type="button">Build one</button>}
        action={<button type="button">See an example</button>}
      >
        Pick any analysis.
      </EmptyState>,
    );
    expect(screen.getByRole("heading", { name: "Your first dashboard starts from a finished run." })).toBeInTheDocument();
    expect(screen.getAllByRole("button").map((b) => b.textContent)).toEqual(["Build one", "See an example"]);
    const card = document.querySelector("[data-slot=empty-state]")!;
    expect(card.querySelector("[data-slot=illustration]")).toHaveAttribute("aria-hidden", "true");
  });

  it("shows a primary action on the plain card too", () => {
    render(<EmptyState title="Nothing here" primaryAction={<button type="button">Add</button>} />);
    expect(screen.getByRole("button", { name: "Add" })).toBeInTheDocument();
    expect(document.querySelector("[data-slot=empty-state]")).toBeNull();
  });
});
