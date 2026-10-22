/**
 * Layout 2 fixtures and an in-memory {@link DashboardClientV2} for the readout tests (imported
 * by tests only; the v2 mock fixtures of `mocks/dashboards.ts` are WP15's). The numbers are
 * small and fixed so the words and accessible names can be checked exactly.
 */
import type {
  ActualsTemplate,
  BatchTemplate,
  ClientDashboard,
  DashboardClientV2,
  PanelBlock,
  PrincipalKind,
  Readout,
  ReadoutHeader,
  ReadoutSnapshot,
} from "../../types";

export const CREATED = "2026-10-20T09:40:00Z";

/** One 2 x 2 frame of codes 0, 85, 170, 255. */
export const FIELD_CODES = btoa(String.fromCharCode(0, 85, 170, 255));

export const CLASSIFIER_BLOCKS: PanelBlock[] = [
  {
    kind: "kpi",
    panel_id: "chance",
    title: "Chance of churn",
    tile: { key: "panel", label: "Chance of churn", value: 0.3, low: 0.2, high: 0.4, unit: "probability", panel_id: "chance", delta: 0.1, delta_basis: "the readout before", delta_dir: "up", caption: "" },
  },
  { kind: "kpi", panel_id: "expected", title: "Expected churners", approximate: true, tile: { key: "panel", label: "Expected churners", value: 12, unit: "count", panel_id: "expected", approximate: true } },
  { kind: "bars", panel_id: "probs", title: "Probability of each result", intent: "probability_bars", rows: [{ label: "yes", value: 0.3 }, { label: "no", value: 0.7 }] },
  { kind: "scatter", panel_id: "fit", title: "Predicted and actual", intent: "scatter", points: [{ x: 1, y: 1.1 }, { x: 2, y: 1.9 }, { x: 3, y: 3.2 }] },
  { kind: "calibration", panel_id: "calib", title: "Calibration", bins: [{ p_mean: 0.1, observed: 0.15, n: 20 }, { p_mean: 0.8, observed: 0.6, n: 10 }] },
  { kind: "confusion", panel_id: "conf", title: "Right and wrong", labels: ["no", "yes"], counts: [[8, 2], [1, 4]] },
  { kind: "field", panel_id: "map", title: "Risk map", width: 2, height: 2, frames: [{ label: "2026", codes_b64: FIELD_CODES }, { label: "2027", codes_b64: FIELD_CODES }], vmin: 0, vmax: 10, units: "cases" },
  { kind: "table", panel_id: "tbl", title: "Next cases", columns: ["case", "chance"], rows: [["c1", 0.25], ["c2", null]] },
  { kind: "note", panel_id: "note", title: "About this model", text: "Line one.\nLine two." },
  { kind: "picture", panel_id: "pic", title: "Training curve", asset_id: "dsa_01jaf3w1abcdefghjkmnpqrstv", alt: "The loss falls over 20 epochs.", width: 640, height: 400 },
  { kind: "unavailable", panel_id: "gone", title: "Feature importance", reason: "This model gives no feature importance." },
  {
    kind: "series",
    panel_id: "two",
    title: "Two lines",
    intent: "line",
    series: [
      { name: "a", label: "A", points: [{ t: "2026-01-01", mean: 1 }, { t: "2026-02-01", mean: 2 }] },
      { name: "b", label: "B", points: [{ t: "2026-01-01", mean: 3 }, { t: "2026-02-01", mean: 1 }] },
    ],
  },
];

export function snapshot(over: Partial<ReadoutSnapshot> = {}): ReadoutSnapshot {
  return { lead: "The model gives a 30% chance of churn for this case.", schema_version: 2, preset: "classifier", panels: CLASSIFIER_BLOCKS, tiles: [], channels: [], series: [], ...over };
}

export function readout(seq: number, over: Partial<Readout> = {}): Readout {
  return {
    seq,
    kind: "baseline",
    trigger: "publish",
    status: "ready",
    visibility: "published",
    title: `Readout ${seq}`,
    created_at: CREATED,
    ready_at: CREATED,
    model_ref: "churn@3",
    model_version_id: "mv_1",
    period: null,
    snapshot: snapshot(),
    ...over,
  };
}

export function classifierDashboard(over: Partial<ClientDashboard> = {}): ClientDashboard {
  return {
    id: "dsh_classifier",
    title: "Churn risk",
    client_name: "Acme",
    summary: "Which customers will leave.",
    note: null,
    period_label: "case",
    kpi: { variable: "churn", label: "Churn", unit: "percent", currency: null, decimals: 0, value_per_unit: null },
    channels: [],
    tiles: [],
    tabs: [],
    inputs: { whatif: false, exact: false, actuals: true, actuals_mode: "compared", refit_requests: false, scenario_rows: true, batch: true, accuracy_kind: "classification" },
    live_inputs: "ok",
    show_technical: false,
    latest_readout: readout(3),
    ends_at: null,
    layout_version: 2,
    preset: "classifier",
    subject: { variable: "churn", label: "Churn", unit: "probability", decimals: 0, positive_label: "yes" },
    panels_meta: [],
    scenario_form: {
      max_rows: 2,
      outputs: ["churn"],
      inputs: [
        { name: "tenure", label: "Months as a customer", type: "integer", min: 0, max: 120, step: 1, default: 12, bind: { var: "tenure" } },
        { name: "plan", label: "Plan", type: "choice", choices: ["basic", "pro"], default: "basic", bind: { var: "plan" } },
      ],
      horizon: null,
    },
    batch_form: { max_rows: 3, id_column: "case_id", columns: ["case_id", "tenure", "plan"], outputs: ["churn"] },
    sections: ["overview", "try", "accuracy", "batch", "history"],
    upgrade_note: null,
    ...over,
  };
}

export const BATCH_TEMPLATE: BatchTemplate = {
  id_column: "case_id",
  max_rows: 3,
  example_csv: "case_id,tenure,plan\ncase1,12,basic\ncase2,13,pro\n",
  columns: [
    { name: "case_id", role: "id", type: "text" },
    { name: "tenure", role: "driver", type: "integer", min: 0, max: 120 },
    { name: "plan", role: "driver", type: "choice", choices: ["basic", "pro"] },
  ],
};

export const ROW_TEMPLATE: ActualsTemplate = {
  mode: "compared",
  match: "row",
  id_column: "case_id",
  label_column: "churned",
  kpi_column: "churned",
  max_rows: 100,
  example_csv: "case_id,tenure,churned\nc1,12,yes\n",
  input_table: {
    columns: [
      { name: "case_id", role: "coord", dims: [] },
      { name: "tenure", role: "var", dims: ["case_id"] },
      { name: "churned", role: "var", dims: ["case_id"] },
    ],
  },
} as unknown as ActualsTemplate;

export interface FakeClient extends DashboardClientV2 {
  calls: { method: string; args: unknown[] }[];
  readouts_: Map<number, Readout>;
}

/** An in-memory client: every POST makes a ready readout at once with the given snapshot. */
export function fakeClient(dashboard: ClientDashboard, opts: { kind?: PrincipalKind; actualsTemplate?: ActualsTemplate; onPredict?: () => Promise<never> | null } = {}): FakeClient {
  const readouts = new Map<number, Readout>();
  for (const r of [dashboard.latest_readout].filter(Boolean) as Readout[]) readouts.set(r.seq, r);
  let next = 10;
  const calls: FakeClient["calls"] = [];
  const header = (r: Readout): ReadoutHeader => {
    const { snapshot: _s, problem: _p, ...h } = r;
    return h;
  };
  const make = (over: Partial<Readout>): Readout => {
    const r = readout(next++, { visibility: "private", ...over });
    readouts.set(r.seq, r);
    return r;
  };
  const log = (method: string, ...args: unknown[]) => calls.push({ method, args });
  return {
    kind: opts.kind ?? "client",
    cacheKey: ["test", dashboard.id, opts.kind ?? "client"],
    calls,
    readouts_: readouts,
    dashboard: () => Promise.resolve(dashboard),
    readouts: () => Promise.resolve([...readouts.values()].map(header)),
    readoutPage: () => Promise.resolve({ items: [...readouts.values()].sort((a, b) => b.seq - a.seq).map(header), next_before_seq: null }),
    readout: (seq) => {
      const r = readouts.get(seq);
      return r ? Promise.resolve(r) : Promise.reject(Object.assign(new Error("not found"), { status: 404 }));
    },
    grid: () => Promise.resolve(null),
    evaluate: () => Promise.reject(new Error("no what-ifs")),
    saveScenario: () => Promise.reject(new Error("no scenarios")),
    actualsTemplate: () => Promise.resolve(opts.actualsTemplate ?? ROW_TEMPLATE),
    submitActuals: (body, key) => {
      log("submitActuals", body, key);
      const r = make({
        kind: "actuals",
        trigger: "actuals",
        visibility: "pending_review",
        snapshot: snapshot({ lead: "On the 2 cases you sent, the model was right 50% of the time.", accuracy_detail: { kind: "classification", n: 2, accuracy: 0.5, brier: 0.2, positive: "yes", threshold: 0.5 } }),
      });
      return Promise.resolve(header(r));
    },
    predict: (body, key) => {
      log("predict", body, key);
      const refused = opts.onPredict?.();
      if (refused) return refused;
      const r = make({
        kind: "prediction",
        trigger: "scenario_rows",
        snapshot: snapshot({
          lead: "The model gives a 42% chance of churn for this case.",
          prediction: { band: "mean", rows: body.rows.map((row) => ({ inputs: row, outputs: { churn: { centre: 0.42, low: 0.3, high: 0.55, label: "no", probability: 0.58 } } })) },
        }),
      });
      return Promise.resolve(r);
    },
    batchTemplate: () => Promise.resolve(BATCH_TEMPLATE),
    submitBatch: (body, key) => {
      log("submitBatch", body, key);
      const r = make({
        kind: "batch",
        trigger: "batch",
        snapshot: snapshot({
          lead: "The model scored 2 cases.",
          batch: {
            n_rows: 2,
            n_scored: 2,
            outputs: ["churn"],
            share_positive: 0.5,
            stats: { churn: { mean: 0.4, min: 0.1, p10: 0.12, p50: 0.4, p90: 0.7, max: 0.75 } },
            histogram: [{ lo: 0, hi: 0.5, count: 1 }, { lo: 0.5, hi: 1, count: 1 }],
            class_counts: [{ label: "yes", count: 1 }, { label: "no", count: 1 }],
            preview: [{ case_id: "a", churn: 0.1 }],
            download: { available: true, rows: 2, bytes_estimate: 80 },
          },
        }),
      });
      return Promise.resolve(r);
    },
    downloadPredictions: (seq) => {
      log("downloadPredictions", seq);
      return Promise.resolve(new Blob(["case_id,churn\na,0.1\n"], { type: "text/csv" }));
    },
    asset: (id) => {
      log("asset", id);
      return Promise.resolve(new Blob([new Uint8Array([137, 80, 78, 71])], { type: "image/png" }));
    },
    keep:
      opts.kind === "share"
        ? null
        : (seq) => {
            log("keep", seq);
            const r = readouts.get(seq)!;
            const kept = { ...r, visibility: "pending_review" as const };
            readouts.set(seq, kept);
            return Promise.resolve(header(kept));
          },
  };
}
