import { describe, expect, it } from "vitest";

import toolsGen from "@/api/dashboard-tools.gen.json";
import { qk } from "@/api/queries";

import { dk, memberCacheKey, shareCacheKey } from "./keys";
import {
  callTool,
  compareReadouts,
  DASHBOARD_TOOL_NAMES,
  explainMetric,
  getAccuracy,
  getBatchSummary,
  getChannel,
  getLatestReadout,
  getPanel,
  getPrediction,
  GLOSSARY,
  listInputs,
  listPanels,
  listReadouts,
  ToolArgsError,
  ToolNotFoundError,
  ToolRefusedError,
} from "./tools";
import { GLOSSARY_KEYS_V2, type ClientDashboard, type DashboardClient, type KpiBlock, type Readout, type ReadoutHeader, type ReadoutQuery } from "./types";

interface GenTool {
  name: string;
  description: string;
  parameters: { properties: Record<string, { enum?: string[] }>; required?: string[] };
  result: { title?: string; examples?: { text?: string }[] };
}
const GEN = toolsGen as unknown as GenTool[];

describe("drift against mapi_spec.dashboard_tools (dashboard-tools.gen.json)", () => {
  it("mirrors the tool names in order", () => {
    expect(GEN.map((t) => t.name)).toEqual([...DASHBOARD_TOOL_NAMES]);
  });

  it("knows every argument of every tool", () => {
    const known: Record<string, string[]> = {
      get_dashboard: [],
      get_latest_readout: [],
      list_readouts: ["before_seq", "kind", "limit"],
      get_readout: ["seq"],
      compare_readouts: ["a_seq", "b_seq"],
      get_channel: ["last_n", "name"],
      explain_metric: ["metric"],
      evaluate_whatif: ["multipliers"],
      list_panels: [],
      get_panel: ["panel_id", "seq"],
      get_accuracy: ["seq"],
      list_inputs: [],
      get_prediction: ["seq"],
      get_batch_summary: ["seq"],
    };
    for (const t of GEN) expect(Object.keys(t.parameters.properties).sort(), t.name).toEqual(known[t.name]);
  });

  it("has the glossary's keys and texts", () => {
    const explain = GEN.find((t) => t.name === "explain_metric")!;
    expect(explain.parameters.properties.metric?.enum).toEqual([...GLOSSARY_KEYS_V2]);
    expect(Object.keys(GLOSSARY).sort()).toEqual([...GLOSSARY_KEYS_V2].sort());
    expect(explain.result.examples?.[0]?.text).toBe(GLOSSARY.return_on_media);
  });

  it("marks the same arguments required", () => {
    const required: Record<string, string[]> = { get_readout: ["seq"], compare_readouts: ["a_seq", "b_seq"], get_channel: ["name"], explain_metric: ["metric"], get_panel: ["panel_id"], get_prediction: ["seq"], get_batch_summary: ["seq"] };
    for (const t of GEN) expect([...(t.parameters.required ?? [])].sort(), t.name).toEqual(required[t.name] ?? []);
  });
});

describe("query keys", () => {
  it("nest under qk's dashboard keys", () => {
    expect(dk.all("acme/growth")).toEqual(qk.dashboards("acme/growth"));
    expect(dk.one("acme/growth", "dsh_1")).toEqual(qk.dashboard("acme/growth", "dsh_1"));
    expect(memberCacheKey("acme/growth", "dsh_1", "client").slice(0, 4)).toEqual(qk.dashboard("acme/growth", "dsh_1"));
    expect(shareCacheKey("dsh_1")[0]).toBe("share");
    expect(dk.viewReadout(shareCacheKey("dsh_1"), 3)).toEqual(["share", "dsh_1", "view", "readout", 3]);
  });
});

// ------------------------------------------------------------------------------ a fake client

const DASH: ClientDashboard = {
  id: "dsh_1",
  title: "T",
  kpi: { variable: "orders", label: "Orders", unit: "count", decimals: 0 },
  channels: [
    { name: "tv", label: "TV" },
    { name: "search", label: "Search" },
  ],
  tiles: ["expected"],
  tabs: ["this_period"],
  latest_readout: null,
};

function readout(seq: number, patch: Partial<Readout> = {}, expected = 1000 + seq * 10, roi = 1 + seq / 10): Readout {
  return {
    seq,
    kind: "refresh",
    trigger: "workflow",
    visibility: "published",
    status: "ready",
    title: `R${seq}`,
    model_version_id: seq > 50 ? "mv_b" : "mv_a",
    model_ref: "model://x@v1",
    created_at: `2026-10-${String(1 + (seq % 28)).padStart(2, "0")}T09:00:00Z`,
    snapshot: {
      lead: "x",
      tiles: [{ key: "expected", label: "Expected orders", value: expected, unit: "count" }],
      channels: [{ name: "tv", label: "TV", spend: 100, contribution: 100 * roi, roi, zone: "optimal" }],
    },
    ...patch,
  };
}

function fakeClient(rows: Readout[], dash: ClientDashboard = DASH): DashboardClient & { reads: number[]; pages: ReadoutQuery[] } {
  const byNewest = [...rows].sort((a, b) => b.seq - a.seq);
  const header = (r: Readout): ReadoutHeader => ({ ...r });
  const c = {
    kind: "member" as const,
    cacheKey: ["fake"],
    reads: [] as number[],
    pages: [] as ReadoutQuery[],
    dashboard: () => Promise.resolve(dash),
    readoutPage: (q: ReadoutQuery = {}) => {
      c.pages.push(q);
      const match = byNewest.filter((r) => (q.beforeSeq === undefined || r.seq < q.beforeSeq) && (q.kind === undefined || r.kind === q.kind));
      const items = match.slice(0, q.limit ?? 20);
      const more = match.length > items.length;
      return Promise.resolve({ items: items.map(header), next_before_seq: more ? (items.at(-1)?.seq ?? null) : null });
    },
    readouts: async (q?: ReadoutQuery) => (await c.readoutPage(q)).items ?? [],
    readout: (seq: number) => {
      c.reads.push(seq);
      const r = rows.find((x) => x.seq === seq);
      return r ? Promise.resolve(r) : Promise.reject(new Error("404"));
    },
    grid: () => Promise.resolve(null),
    evaluate: (m: Record<string, number>) => Promise.resolve({ tiles: [], channels: [], delta_kpi: Object.values(m).length, lead: "ok" }),
    saveScenario: () => Promise.reject(new Error("tools never write")),
    actualsTemplate: () => Promise.reject(new Error("tools never read templates")),
    submitActuals: () => Promise.reject(new Error("tools never write")),
  };
  return c;
}

describe("tools over a client", () => {
  it("compares two readouts with the server's lead", async () => {
    const c = fakeClient([readout(11, {}, 4090, 1.9), readout(12, {}, 4210, 1.86)]);
    const cmp = await compareReadouts(c, { a_seq: 11, b_seq: 12 });
    expect(cmp.same_model).toBe(true);
    expect(cmp.tiles[0]).toMatchObject({ key: "expected", a: 4090, b: 4210, delta: 120 });
    expect(cmp.channels).toEqual([expect.objectContaining({ name: "tv", roi_a: 1.9, roi_b: 1.86 })]);
    expect(cmp.lead).toBe("Readout 12 expects 120 more orders than readout 11.");
    expect(cmp.b).not.toHaveProperty("snapshot");

    const fewer = await compareReadouts(fakeClient([readout(1, {}, 500), readout(60, {}, 400)]), { a_seq: 1, b_seq: 60 });
    expect(fewer.lead).toBe("Readout 60 expects 100 fewer orders than readout 1. The model changed between the two readouts.");
    expect(fewer.same_model).toBe(false);
  });

  it("follows one channel over published, ready readouts, oldest first, paging back", async () => {
    const rows = Array.from({ length: 70 }, (_, i) => readout(i + 1));
    rows[69] = readout(70, { visibility: "pending_review" });
    rows[68] = readout(69, { status: "failed", snapshot: null });
    const c = fakeClient(rows);
    const h = await getChannel(c, { name: "tv", last_n: 26 });
    expect(h.points).toHaveLength(26);
    expect(h.points.at(-1)?.seq).toBe(68);
    expect(h.points[0]?.seq).toBe(43);
    expect(c.reads).not.toContain(70);
    await expect(getChannel(c, { name: "radio" })).rejects.toBeInstanceOf(ToolNotFoundError);
    await expect(getChannel(c, { name: "tv", last_n: 27 })).rejects.toBeInstanceOf(ToolArgsError);
  });

  it("checks arguments as the server does", async () => {
    const c = fakeClient([readout(1)]);
    await expect(listReadouts(c, { limit: 51 })).rejects.toBeInstanceOf(ToolArgsError);
    await expect(callTool(c, "get_readout", { seq: 0 })).rejects.toBeInstanceOf(ToolArgsError);
    await expect(callTool(c, "submit_actuals")).rejects.toBeInstanceOf(ToolArgsError);
    await expect(getLatestReadout(c)).rejects.toBeInstanceOf(ToolNotFoundError);
    expect(() => explainMetric({ metric: "nonsense" as never })).toThrow(ToolArgsError);
  });

  it("answers every tool by name without writing", async () => {
    const c = fakeClient([readout(1), readout(2)], { ...DASH, latest_readout: readout(2) });
    expect(await callTool(c, "get_dashboard")).toBe(await c.dashboard());
    expect(await callTool(c, "get_latest_readout")).toMatchObject({ seq: 2 });
    expect(await callTool(c, "list_readouts", { limit: 1 })).toMatchObject({ items: [{ seq: 2 }], next_before_seq: 2 });
    expect(await callTool(c, "get_readout", { seq: 1 })).toMatchObject({ seq: 1 });
    expect(await callTool(c, "compare_readouts", { a_seq: 1, b_seq: 2 })).toHaveProperty("lead");
    expect(await callTool(c, "get_channel", { name: "tv" })).toMatchObject({ name: "tv", label: "TV" });
    expect(await callTool(c, "explain_metric", { metric: "zones" })).toEqual({ metric: "zones", text: GLOSSARY.zones });
    expect(await callTool(c, "evaluate_whatif", { multipliers: { tv: 1.2 } })).toMatchObject({ delta_kpi: 1 });
    expect(await callTool(c, "list_panels")).toEqual({ panels: [] });
    expect(await callTool(c, "get_accuracy")).toEqual({ seq: null, detail: null, history: [] });
  });
});

// ------------------------------------------------------------------------------ layout 2

const DASH2: ClientDashboard = {
  id: "dsh_2",
  title: "Churn",
  kpi: { variable: "churn", label: "Churn", unit: "ratio", decimals: 2 },
  subject: { variable: "churn", label: "Churn", unit: "probability", decimals: 2 },
  layout_version: 2,
  preset: "classifier",
  channels: [],
  panels_meta: [
    { id: "share", kind: "kpi", title: "Share above threshold", caption: null, intent: null },
    { id: "cal", kind: "chart", title: "Calibration", caption: null, intent: "calibration" },
  ],
  inputs: { scenario_rows: true, batch: true, actuals: true, accuracy_kind: "classification" },
  scenario_form: { inputs: [] } as unknown as ClientDashboard["scenario_form"],
  latest_readout: null,
};

function kpi(panel_id: string, title: string, value: number | null): KpiBlock {
  return { kind: "kpi", panel_id, title, tile: { key: "panel", label: title, value, unit: "percent" } };
}

function readout2(seq: number, patch: Partial<Readout> = {}, share = 10 + seq): Readout {
  return {
    seq,
    kind: "refresh",
    trigger: "workflow",
    visibility: "published",
    status: "ready",
    title: `R${seq}`,
    model_version_id: "mv_a",
    model_ref: "model://x@v1",
    created_at: "2026-10-20T09:00:00Z",
    snapshot: { schema_version: 2, lead: "x", tiles: [{ key: "panel", label: "Share", value: share, unit: "percent" }], channels: [], panels: [kpi("share", "Share above threshold", share)] },
    ...patch,
  };
}

describe("layout 2 tools", () => {
  it("compares KPI panels when there is no expected tile", async () => {
    const c = fakeClient([readout2(3, {}, 12), readout2(4, {}, 15.5)], DASH2);
    const cmp = await compareReadouts(c, { a_seq: 3, b_seq: 4 });
    expect(cmp.tiles).toEqual([]);
    expect(cmp.channels).toEqual([]);
    expect(cmp.panel_deltas).toEqual([{ panel_id: "share", label: "Share above threshold", a: 12, b: 15.5, delta: 3.5 }]);
    expect(cmp.lead).toBe("Share above threshold went up by 3.50 from readout 3 to readout 4.");
    const same = await compareReadouts(fakeClient([readout2(3, {}, 12), readout2(4, {}, 12)], DASH2), { a_seq: 3, b_seq: 4 });
    expect(same.lead).toBe("Share above threshold is the same in readouts 3 and 4.");
    const down = await compareReadouts(fakeClient([readout2(3, {}, 20), readout2(4, {}, 18)], DASH2), { a_seq: 3, b_seq: 4 });
    expect(down.lead).toBe("Share above threshold went down by 2 from readout 3 to readout 4.");
  });

  it("refuses the media tools without the media planner", async () => {
    const c = fakeClient([readout2(1)], DASH2);
    await expect(getChannel(c, { name: "tv" })).rejects.toMatchObject({ code: "DASHBOARD_INPUT_DISABLED" });
    await expect(callTool(c, "evaluate_whatif", { multipliers: {} })).rejects.toBeInstanceOf(ToolRefusedError);
  });

  it("lists panels and inputs from the dashboard view", async () => {
    const c = fakeClient([], DASH2);
    expect((await listPanels(c)).panels.map((p) => p.id)).toEqual(["share", "cal"]);
    const inp = await listInputs(c);
    expect(inp.inputs).toMatchObject({ scenario_rows: true, batch: true });
    expect(inp.scenario_form).toEqual({ inputs: [] });
    expect(inp.batch_form).toBeNull();
  });

  it("reads one panel of a readout or of the latest one", async () => {
    const latest = readout2(5);
    const c = fakeClient([readout2(4), latest, readout2(6, { status: "running", snapshot: null })], { ...DASH2, latest_readout: latest });
    expect(await getPanel(c, { panel_id: "share" })).toMatchObject({ seq: 5, panel: { kind: "kpi", tile: { value: 15 } } });
    expect(await callTool(c, "get_panel", { panel_id: "share", seq: 4 })).toMatchObject({ seq: 4 });
    await expect(getPanel(c, { panel_id: "cal", seq: 4 })).rejects.toBeInstanceOf(ToolNotFoundError);
    await expect(getPanel(c, { panel_id: "share", seq: 6 })).rejects.toMatchObject({ code: "NOT_READY" });
    await expect(getPanel(c, { panel_id: "Share" })).rejects.toBeInstanceOf(ToolArgsError);
    await expect(getPanel(fakeClient([], DASH2), { panel_id: "share" })).rejects.toBeInstanceOf(ToolNotFoundError);
  });

  it("reads accuracy from the latest published actuals readout, and layout 1 accuracy as error metrics", async () => {
    const detail = { kind: "classification", n: 40, accuracy: 0.8 } as const;
    const history = [{ seq: 7, created_at: "2026-10-20T09:00:00Z", n: 40, values: { accuracy: 0.8 } }];
    const actuals = (seq: number, visibility: Readout["visibility"]) => readout2(seq, { kind: "actuals", visibility, snapshot: { ...readout2(seq).snapshot!, accuracy_detail: detail, accuracy_history: history } });
    const c = fakeClient([actuals(7, "published"), actuals(8, "private"), readout2(9)], DASH2);
    expect(await getAccuracy(c)).toEqual({ seq: 7, detail, history });
    expect(c.pages.at(-1)).toMatchObject({ kind: "actuals" });
    const v1 = readout(3, { kind: "actuals", snapshot: { lead: "x", tiles: [], channels: [], accuracy: { n: 4, mape: 5 } } });
    expect(await getAccuracy(fakeClient([v1]), { seq: 3 })).toEqual({ seq: 3, detail: { kind: "error", n: 4, mape: 5 }, history: [] });
    await expect(callTool(c, "get_accuracy", { seq: 0 })).rejects.toBeInstanceOf(ToolArgsError);
  });

  it("reads the caller's prediction and batch summary (never the CSV)", async () => {
    const pred = readout2(11, { kind: "prediction", visibility: "private", snapshot: { ...readout2(11).snapshot!, prediction: { rows: [] } } });
    const batch = readout2(12, { kind: "batch", visibility: "private", snapshot: { ...readout2(12).snapshot!, batch: { n_rows: 2000 } as never } });
    const c = fakeClient([pred, batch], DASH2);
    const p = await getPrediction(c, { seq: 11 });
    expect(p.prediction).toEqual({ rows: [] });
    expect(p.readout).not.toHaveProperty("snapshot");
    const b = await getBatchSummary(c, { seq: 12 });
    expect(b.summary).toEqual({ n_rows: 2000 });
    expect(JSON.stringify(b)).not.toContain("download");
    await expect(getPrediction(c, { seq: 12 })).rejects.toBeInstanceOf(ToolNotFoundError);
    await expect(callTool(c, "get_batch_summary", { seq: 11 })).rejects.toBeInstanceOf(ToolNotFoundError);
  });
});
