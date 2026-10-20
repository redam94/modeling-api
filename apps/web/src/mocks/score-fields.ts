/**
 * A score of a model whose outputs lie on grids (the `demo-fields` model of `explore.ts`):
 * the runner's `ui/fields.json` (the `fields` view of `fields.gen.json`: `intensity` on a
 * 32 × 48 grid over 6 `period` frames, and a vector field), the inline summary with a grid
 * table, a table over time and a single value, and the run's plots with the static field map
 * the page hides. Tests add {@link gridScoreHandlers} with `server.use`, so `handlers.ts`
 * keeps its MMM score.
 */
import { http, HttpResponse, type HttpHandler } from "msw";

import type { PlotSpec, ScoreSummary } from "@/api/types";
import { env } from "@/env";
import type { FieldsView } from "@/features/explore/views";

import fieldsGen from "./fields.gen.json";
import * as fx from "./fixtures";

const API = env.apiBase;
const enc = new TextEncoder();

export const scoreFieldsView = fieldsGen.fit.views.fields as unknown as FieldsView;

const PERIODS = [2019, 2020, 2021, 2022, 2023, 2024];

export const gridScoreSummary: ScoreSummary = {
  tables: [
    // A few cells: the summary of a grid is cut short; the map comes from ui/fields.json.
    {
      variable: "intensity",
      columns: ["period", "row", "col", "mean", "hdi_90_lower", "hdi_90_upper"],
      rows: PERIODS.slice(0, 2).flatMap((p) => [[p, 0, 0, 1.2, 0.4, 2.1], [p, 0, 1, 1.4, 0.5, 2.6]]),
      dims: ["period", "row", "col"],
    },
    {
      variable: "total",
      columns: ["period", "mean", "hdi_90_lower", "hdi_90_upper"],
      rows: PERIODS.map((p, i) => [p, 410 + 12 * i, 360 + 10 * i, 465 + 14 * i]),
      dims: ["period"],
      time_dim: "period",
    },
    { variable: "peak", columns: ["mean", "hdi_90_lower", "hdi_90_upper"], rows: [[9.5, 6.25, 13]], dims: [] },
  ] as ScoreSummary["tables"],
};

/** The run's plots: the forecast fan and the static field map (hidden next to the field viewer). */
export const gridRunPlots: PlotSpec[] = [
  fx.plotSpec,
  { ...fx.plotSpec, id: "sci.field_map", title: "Field map", caption_plain: "The mean of each cell.", provenance: { recipe: "sci.field_map@1.0.0", generated_at: fx.T0 } },
];

/** The score call (with `ui/fields.json` among its outputs) and the run's plots. */
export function gridScoreHandlers(state: { artifacts: Map<string, { bytes: Uint8Array; type: string }> }): HttpHandler[] {
  return [
    http.post(new RegExp(`/v1/models/[^/]+/versions/\\d+:score$`), () => {
      const bytes = enc.encode(JSON.stringify(scoreFieldsView));
      state.artifacts.set("ui/fields.json", { bytes, type: "application/json" });
      const fan = state.artifacts.get("ui/plotdata/ppc_fan.arrow")!;
      return HttpResponse.json({
        done: true,
        run: { ...fx.runDone, id: "run_grid", kind: "score", model_version_id: fx.MV_ID, spec_id: null, spec_revision: null },
        outputs: [fx.artifactLink("ui/fields.json", bytes, "application/json"), fx.artifactLink("ui/plotdata/ppc_fan.arrow", fan.bytes, fan.type)],
        summary: gridScoreSummary,
      });
    }),
    http.get(`${API}/v1/runs/:id/plots`, ({ params }) => (params.id === "run_grid" ? HttpResponse.json({ items: gridRunPlots, next_cursor: null }) : undefined)),
  ];
}
