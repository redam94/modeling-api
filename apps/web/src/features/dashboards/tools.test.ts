import { describe, expect, it } from "vitest";

import toolsGen from "@/api/dashboard-tools.gen.json";
import { qk } from "@/api/queries";

import { dk, memberCacheKey, shareCacheKey } from "./keys";
import { callTool, compareReadouts, DASHBOARD_TOOL_NAMES, explainMetric, getChannel, getLatestReadout, GLOSSARY, listReadouts, ToolArgsError, ToolNotFoundError } from "./tools";
import { GLOSSARY_KEYS, type ClientDashboard, type DashboardClient, type Readout, type ReadoutHeader, type ReadoutQuery } from "./types";

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
    };
    for (const t of GEN) expect(Object.keys(t.parameters.properties).sort(), t.name).toEqual(known[t.name]);
  });

  it("has the glossary's keys and texts", () => {
    const explain = GEN.find((t) => t.name === "explain_metric")!;
    expect(explain.parameters.properties.metric?.enum).toEqual([...GLOSSARY_KEYS]);
    expect(Object.keys(GLOSSARY).sort()).toEqual([...GLOSSARY_KEYS].sort());
    expect(explain.result.examples?.[0]?.text).toBe(GLOSSARY.return_on_media);
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
      const items = byNewest.filter((r) => q.beforeSeq === undefined || r.seq < q.beforeSeq).slice(0, q.limit ?? 20);
      const more = byNewest.filter((r) => q.beforeSeq === undefined || r.seq < q.beforeSeq).length > items.length;
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
  });
});
