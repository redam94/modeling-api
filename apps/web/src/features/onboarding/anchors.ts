/**
 * Tour anchors (v0.18 D18, I-8): every place a tour step can point at. A page marks the place
 * with a literal `data-tour` attribute (WP-T7 adds most of them), and a tour step
 * names the anchor. `anchors.test.ts` keeps the two sides in step: every anchor a tour uses
 * is listed here, and every `data-tour` literal in `src/` is a listed anchor.
 *
 * Write the attribute as a literal in the markup (`data-tour="home.checklist"`), never built
 * from a variable: the static test reads the source text.
 *
 * Imports nothing, so any signed-in chunk may use the names.
 */

export const TOUR_ANCHORS = [
  "home.start-here",
  "home.checklist",
  "home.playground",
  "nav.primary",
  "nav.guided",
  "nav.datasets",
  "nav.dashboards",
  "nav.catalog",
  "nav.make-your-own",
  "nav.more",
  "shell.help-menu",
  "guided.templates",
  "guided.data.upload",
  "guided.data.mapping",
  "guided.run.progress",
  "guided.run.chains",
  "guided.report.headline",
  "guided.report.scorecard",
  "guided.report.plots",
  "guided.explore.sliders",
  "guided.explore.result",
  "guided.explore.exact",
  "sample.progress",
  "catalog.playground",
  "dashboards.new.presets",
  "dashboards.builder.panels",
  "dashboards.publish",
  "dashboards.share",
  "catalog.browse",
  "catalog.tab.plots",
  "catalog.recipe.try",
  "catalog.recipe.settings",
  "catalog.recipe.yaml",
  "extend-add-own",
  "extend-no-code",
  "extend-with-code",
  "library.new-model",
  "library.prior",
  "library.use",
  "library.chart-preview",
  "workflows.save-as-template",
] as const;

export type TourAnchor = (typeof TOUR_ANCHORS)[number];

const KNOWN: ReadonlySet<string> = new Set(TOUR_ANCHORS);

export function isTourAnchor(value: unknown): value is TourAnchor {
  return typeof value === "string" && KNOWN.has(value);
}

/** The CSS selector of an anchor (anchor names hold no quotes or backslashes). */
export function anchorSelector(anchor: TourAnchor): string {
  return `[data-tour=${JSON.stringify(anchor)}]`;
}

/** A layout box is drawn when it has an area (a `display: none` copy has none). */
function hasBox(el: Element): boolean {
  const r = el.getBoundingClientRect();
  return r.width > 0 || r.height > 0;
}

/**
 * The element an anchor names, or `null` when it is not on screen. A page can hold the same
 * anchor twice (the side rail and the phone nav bar): the first one with a layout box wins,
 * so the hidden copy is never pointed at.
 */
export function findAnchor(anchor: TourAnchor, root: ParentNode = document): HTMLElement | null {
  const nodes = root.querySelectorAll<HTMLElement>(anchorSelector(anchor));
  for (const el of nodes) if (el.isConnected && hasBox(el)) return el;
  return null;
}
