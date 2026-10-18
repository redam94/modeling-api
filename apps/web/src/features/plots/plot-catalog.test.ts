import { describe, expect, it } from "vitest";

import type { PlotCatalogEntry } from "@/api/types";

import { filterPlots, latestPlots, plotPack } from "./plot-catalog";

const entry = (id: string, version = "1.0.0", extra: Partial<PlotCatalogEntry> = {}): PlotCatalogEntry =>
  ({ id, version, title: id.split(".")[1] ?? id, persona: "both", requires_groups: [], requires_representation: [], yanked: false, ...extra });

describe("plot catalog", () => {
  it("keeps the latest unyanked version of each recipe, by pack then title", () => {
    const plots = latestPlots([
      entry("sci.ensemble"),
      entry("core.ppc", "1.0.0"),
      entry("core.ppc", "1.10.0"),
      entry("core.ppc", "2.0.0", { yanked: true }),
      entry("acme.custom"),
      entry("mmm.budget_plans"),
    ]);
    expect(plots.map((p) => `${p.id}@${p.version}`)).toEqual(["core.ppc@1.10.0", "mmm.budget_plans@1.0.0", "sci.ensemble@1.0.0", "acme.custom@1.0.0"]);
  });

  it("filters by pack and by every word of the text (About included)", () => {
    const about = { title: "Fit and forecast", summary_plain: "Shows the forecast with a band.", use_when: ["You want to see the holdout"], outputs: ["A line"] } as PlotCatalogEntry["about"];
    const plots = [entry("core.ppc_fan", "1.0.0", { about }), entry("mmm.response_curves", "1.0.0", { summary: "Outcome per spend level" })];
    expect(filterPlots(plots, "holdout band", "all").map((p) => p.id)).toEqual(["core.ppc_fan"]);
    expect(filterPlots(plots, "spend", "all").map((p) => p.id)).toEqual(["mmm.response_curves"]);
    expect(filterPlots(plots, "", "mmm").map((p) => p.id)).toEqual(["mmm.response_curves"]);
    expect(plotPack("sci.field_map")).toBe("sci");
  });
});
