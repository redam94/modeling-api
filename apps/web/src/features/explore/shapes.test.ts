/**
 * The Data Explorer's section list from the indexes (`shapes.ts`): an index from before v0.10
 * keeps the thirteen sections; an index with `order` and `reason` orders the list by
 * relevance and moves the sections that do not apply to "Not for this data".
 */
import { describe, expect, it } from "vitest";

import { mergeSections, type Exploration } from "./api";
import { CLASSIC_ORDER, DATA_ORDER } from "./sections-meta";
import { isAdaptive, missingSections, navPlan, runIndex, sectionOrder, shapeSection, shapeWord } from "./shapes";
import { DATA_SECTIONS, SCIENCE_SECTIONS, type ExplorerIndexView } from "./views";

const ref = (section: string, extra: Record<string, unknown> = {}) => ({ section, view: `ui/explore/${section}.json`, ...extra });

/** An index as an ingest of v0.5 writes it (the cheap sections, no order, no reason). */
const OLD: ExplorerIndexView = {
  format: "mapi.explorer-index/v1",
  target: "dataset",
  ref: "dataset://default/sales@v1",
  sections: ["overview", "distributions", "timeseries", "missingness", "outliers", "correlation", "quality"].map((s) => ref(s)),
};

/** An index of v0.10 for an image movie (e07): the relevance order and the sections that do not apply. */
const NA = "Not available: this view does not apply to this data.";
const NEW: ExplorerIndexView = {
  format: "mapi.explorer-index/v1",
  target: "dataset",
  ref: "dataset://seismology/oklahoma_quakes@v1",
  shapes: ["image", "timeseries"],
  order: ["overview", "structure", "images", "quality", "distributions", "timeseries", "missingness", "outliers", "correlation"],
  sections: [
    ...["overview", "structure", "images", "quality", "distributions", "timeseries", "missingness", "outliers", "correlation"].map((s) => ref(s)),
    ...["map", "network", "tree", "circular", "points", "profiles", "groups", "vif"].map((s) => ref(s, { status: "skipped", reason: "not_applicable", message_plain: `${NA} (${s})` })),
    ref("stl", { status: "skipped", reason: "insufficient_data", message_plain: "Not available: fewer than 16 periods." }),
  ],
};

describe("section list", () => {
  it("keeps the thirteen sections for an index from before v0.10", () => {
    const states = mergeSections(DATA_ORDER, OLD, null);
    const plan = navPlan(states, [OLD]);
    expect(plan.adaptive).toBe(false);
    expect(plan.shown).toEqual([...CLASSIC_ORDER]);
    expect(plan.hidden).toEqual([]);
    expect(plan.shapes).toEqual([]);
    // "compute the remaining views" adds the sections for scientific data
    const missing = missingSections(states, plan);
    expect(missing).toEqual(expect.arrayContaining(["ccf", "stl", ...SCIENCE_SECTIONS]));
    expect(missing).not.toContain("overview");
  });

  it("orders the list by relevance and hides the sections that do not apply", () => {
    const states = mergeSections(DATA_ORDER, NEW, null);
    expect(states.find((s) => s.section === "map")?.reason).toBe("not_applicable");
    expect(states.find((s) => s.section === "images")?.reason).toBeNull();
    const plan = navPlan(states, [NEW]);
    expect(plan.adaptive).toBe(true);
    expect(plan.shapes).toEqual(["image", "timeseries"]);
    expect(plan.shown.slice(0, 4)).toEqual(["overview", "structure", "images", "quality"]);
    // the sections that do not apply leave the list, with their reason
    expect(plan.hidden.map((h) => h.section).sort()).toEqual(["circular", "groups", "map", "network", "points", "profiles", "tree", "vif"]);
    expect(plan.hidden.find((h) => h.section === "map")).toMatchObject({ title: "Map", message: `${NA} (map)` });
    // a section skipped for another reason stays, and so do the ones not computed yet
    expect(plan.shown).toContain("stl");
    expect(plan.shown).toEqual(expect.arrayContaining(["ccf", "acf_pacf", "stationarity"]));
    // every section is in one of the two lists, once
    expect([...plan.shown, ...plan.hidden.map((h) => h.section)].sort()).toEqual([...DATA_SECTIONS].sort());
    expect(missingSections(states, plan)).toEqual(["ccf", "acf_pacf", "stationarity"]);
  });

  it("uses a finished run's index first, else the ingest's", () => {
    const run = { done: true, index: { ...NEW, order: ["overview", "images", "structure"] } } as unknown as Exploration;
    expect(runIndex(run)?.order).toEqual(["overview", "images", "structure"]);
    expect(runIndex({ ...run, done: false })).toBeNull();
    expect(sectionOrder([runIndex(run), NEW]).slice(0, 3)).toEqual(["overview", "images", "structure"]);
    // a run on an old ingest that brings reasons makes the list adaptive
    const states = mergeSections(DATA_ORDER, OLD, run);
    expect(navPlan(states, [runIndex(run), OLD]).adaptive).toBe(true);
  });

  it("knows an adaptive index by its order or a reason, and ignores unknown ids", () => {
    expect(isAdaptive(OLD)).toBe(false);
    expect(isAdaptive(null)).toBe(false);
    expect(isAdaptive({ ...OLD, order: ["overview"] })).toBe(true);
    expect(isAdaptive({ ...OLD, sections: [ref("map", { status: "skipped", reason: "not_applicable" })] })).toBe(true);
    expect(isAdaptive({ ...OLD, sections: [ref("map", { status: "skipped", reason: "because" })] })).toBe(false);
    expect(sectionOrder([{ ...OLD, order: ["evil", "images", "images"] }]).slice(0, 2)).toEqual(["images", "overview"]);
  });

  it("names shapes and links each one to its section", () => {
    expect(shapeWord("areal_map")).toBe("Map of areas");
    expect(shapeWord("something_new")).toBe("something new");
    expect(shapeSection({ kind: "image" })).toBe("images");
    expect(shapeSection({ kind: "vector_field", sections: ["evil", "images"] })).toBe("images");
    expect(shapeSection({ kind: "table" })).toBe("correlation");
    expect(shapeSection({ kind: "unknown" })).toBe("structure");
  });
});
