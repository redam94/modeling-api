import { setupServer } from "msw/node";
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";

import type { ClientDashboard, Dashboard, Readout, ReadoutHeader, ReadoutList, ShareSession } from "@/api/types";
import { env } from "@/env";
import { isLayout2, UPGRADE_NOTE, type BatchTemplate, type DashboardPresets, type DashboardSpecV2, type DashboardUpgrade, type SourceCatalog } from "@/features/dashboards/types";

import { CLASSIFIER_V2, FORECAST_DASHBOARD_ID, FORECAST_V2, MOCK_SHARE_TOKENS, REGRESSION_V2, V2_DASHBOARD_IDS, V2_PROJECT } from "./dashboards";
import { createHandlers } from "./handlers";
import { isClientRoute, MOCK_SESSION_KEY, mockCsrfToken, PERSONAS } from "./tenancy";

const server = setupServer(...createHandlers());
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterAll(() => server.close());
beforeEach(() => localStorage.setItem("mapi.mock.readoutMs", "30"));

type Who = keyof typeof PERSONAS;

async function call<T>(who: Who, method: string, path: string, body?: unknown, headers: Record<string, string> = {}, project = V2_PROJECT): Promise<{ status: number; data: T; headers: Headers }> {
  localStorage.setItem(MOCK_SESSION_KEY, who);
  const res = await fetch(`${env.apiBase}${path}`, {
    method,
    headers: { "X-Mapi-Project": project, "X-CSRF-Token": mockCsrfToken(PERSONAS[who]!), ...(body !== undefined ? { "Content-Type": "application/json" } : {}), ...headers },
    body: body === undefined ? null : JSON.stringify(body),
  });
  const type = res.headers.get("content-type") ?? "";
  const text = await res.text();
  return { status: res.status, headers: res.headers, data: (type.includes("json") && text ? JSON.parse(text) : text) as T };
}

async function shared<T>(session: string, method: string, path: string, body?: unknown, headers: Record<string, string> = {}): Promise<{ status: number; data: T; headers: Headers }> {
  const res = await fetch(`${env.apiBase}${path}`, {
    method,
    headers: { "X-Mapi-Share": session, ...(body !== undefined ? { "Content-Type": "application/json" } : {}), ...headers },
    body: body === undefined ? null : JSON.stringify(body),
  });
  const type = res.headers.get("content-type") ?? "";
  const text = await res.text();
  return { status: res.status, headers: res.headers, data: (type.includes("json") && text ? JSON.parse(text) : text) as T };
}

async function openLink(token: string): Promise<ShareSession> {
  const res = await fetch(`${env.apiBase}/v1/share-sessions`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token }) });
  expect(res.status).toBe(200);
  return (await res.json()) as ShareSession;
}

const wait = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function settled(who: Who, id: string, seq: number): Promise<Readout> {
  for (let i = 0; i < 20; i++) {
    const r = (await call<Readout>(who, "GET", `/v1/dashboards/${id}/readouts/${seq}`)).data;
    if (r.status === "ready" || r.status === "failed") return r;
    await wait(20);
  }
  throw new Error(`readout ${seq} did not settle`);
}

/** Every (low, high) of a KPI tile in a snapshot. */
function kpiRanges(r: Readout): [number | null | undefined, number | null | undefined][] {
  return (r.snapshot?.panels ?? []).flatMap((b) => (b.kind === "kpi" ? [[b.tile.low, b.tile.high] as [number | null | undefined, number | null | undefined]] : []));
}

const D = (key: keyof typeof V2_DASHBOARD_IDS) => `/v1/dashboards/${V2_DASHBOARD_IDS[key]}`;

describe("layout 2 fixtures (acme/retail)", () => {
  it("show a client every family, each a layout 2 view", async () => {
    const list = await call<{ items: { id: string }[] }>("cleo", "GET", "/v1/dashboards");
    expect(list.data.items.map((d) => d.id).sort()).toEqual(Object.values(V2_DASHBOARD_IDS).sort());
    for (const key of Object.keys(V2_DASHBOARD_IDS) as (keyof typeof V2_DASHBOARD_IDS)[]) {
      const d = (await call<ClientDashboard>("cleo", "GET", D(key))).data;
      expect(d.layout_version, key).toBe(2);
      expect(d.sections?.[0]).toBe("overview");
      expect(d.panels_meta?.length).toBeGreaterThan(0);
      expect(d.latest_readout?.snapshot?.schema_version).toBe(2);
      // the readout has one block per panel (planner parts excepted), in order
      const ids = (d.panels_meta ?? []).filter((p) => p.kind !== "mmm").map((p) => p.id);
      expect(d.latest_readout?.snapshot?.panels?.map((b) => b.panel_id)).toEqual(ids);
    }
  });

  it("give each preset the inputs its spec allows (no image scoring for clients)", async () => {
    const cls = (await call<ClientDashboard>("cleo", "GET", D("classifier"))).data;
    expect(cls.inputs).toMatchObject({ scenario_rows: true, batch: true, actuals: true, accuracy_kind: "classification", whatif: false });
    expect(cls.sections).toEqual(["overview", "try", "accuracy", "batch", "history"]);
    expect(cls.scenario_form?.inputs?.map((i) => i.name)).toEqual(["price", "promo", "store", "week"]);
    expect(cls.batch_form).toMatchObject({ id_column: "store" });
    expect((await call<ClientDashboard>("cleo", "GET", D("image"))).data.inputs?.batch).toBe(false);
    const asAnalyst = (await call<Dashboard>("ana", "GET", D("image"))).data;
    expect(isLayout2(asAnalyst.draft_spec)).toBe(true);
    expect((await call<BatchTemplate>("ana", "GET", `${D("image")}/batch/template`)).status).toBe(200);
    const causal = (await call<ClientDashboard>("cleo", "GET", D("causal"))).data;
    expect(causal.inputs).toMatchObject({ actuals: true, accuracy_kind: "effect", scenario_rows: false });
  });

  it("refuse the planner's inputs on other presets", async () => {
    for (const p of ["grid", "scenarios"]) {
      const r = await call<{ code: string }>("cleo", p === "grid" ? "GET" : "POST", `${D("forecast")}/${p}`, p === "grid" ? undefined : { multipliers: {} });
      expect(r.data.code, p).toBe("DASHBOARD_INPUT_DISABLED");
    }
  });

  it("keep the automatically upgraded MMM: layout 1 history, then layout 2 with wider ranges and a note", async () => {
    const d = (await call<ClientDashboard>("cleo", "GET", D("mmm"))).data;
    expect(d.preset).toBe("mmm_planner");
    expect(d.upgrade_note?.startsWith(UPGRADE_NOTE)).toBe(true);
    expect(d.channels?.map((c) => c.name)).toEqual(["tv", "search", "social", "display"]);
    expect(d.inputs).toMatchObject({ whatif: true, exact: true });
    const list = (await call<ReadoutList>("cleo", "GET", `${D("mmm")}/readouts`)).data.items ?? [];
    const v2 = await call<Readout>("cleo", "GET", `${D("mmm")}/readouts/${list[0]!.seq}`);
    const v1 = await call<Readout>("cleo", "GET", `${D("mmm")}/readouts/${list[1]!.seq}`);
    expect(v2.data.snapshot?.schema_version).toBe(2);
    expect(v1.data.snapshot?.schema_version ?? 1).toBe(1);
    const width = (r: Readout) => {
      const t = r.snapshot?.tiles?.find((x) => x.key === "expected");
      return (t?.high ?? 0) - (t?.low ?? 0);
    };
    expect(width(v2.data)).toBeGreaterThan(width(v1.data));
    const revs = (await call<{ items: { seq: number; spec: { layout_version?: number } }[] }>("ana", "GET", `${D("mmm")}/revisions`)).data.items;
    expect(revs.map((r) => [r.seq, r.spec.layout_version])).toEqual([
      [2, 2],
      [1, 1],
    ]);
    // the planner's what-if still answers on the upgraded dashboard
    expect((await call("cleo", "POST", `${D("mmm")}/whatif:evaluate`, { multipliers: { tv: 1.1 } })).status).toBe(200);
  });

  it("carry real field codes and ranges that are not sums of bounds", async () => {
    const field = (await call<ClientDashboard>("cleo", "GET", D("field"))).data.latest_readout!;
    const map = field.snapshot?.panels?.find((b) => b.kind === "field");
    expect(map?.kind).toBe("field");
    if (map?.kind === "field") for (const f of map.frames) expect(atob(f.codes_b64).length).toBe(map.width * map.height);
    const fc = (await call<ClientDashboard>("cleo", "GET", D("forecast"))).data.latest_readout!;
    // the forecast total is the run's own band (ForecastView.total), narrower than summed bounds
    const total = fc.snapshot?.panels?.find((b) => b.panel_id === "horizon_total");
    const table = fc.snapshot?.panels?.find((b) => b.panel_id === "next_periods");
    if (total?.kind === "kpi" && table?.kind === "table") {
      const lows = (table.rows ?? []).reduce((a, r) => a + Number(r[2]), 0);
      expect(total.tile.low).toBeGreaterThan(lows);
    } else throw new Error("the forecast fixture lost its total or its table");
    expect(kpiRanges(fc).every(([lo, hi]) => lo == null || hi == null || lo <= hi)).toBe(true);
  });
});

describe("layout 2 builder routes", () => {
  it("suggest presets for a binding, with a compiled spec", async () => {
    const p = await call<DashboardPresets>("ana", "GET", `/v1/dashboards:presets?alias=${FORECAST_V2.source.alias}`);
    expect(p.status).toBe(200);
    expect(p.data.suggestions?.[0]?.preset).toBe("forecast");
    expect(p.data.spec).toMatchObject({ layout_version: 2, preset: "forecast", source: { kind: "endpoint", alias: FORECAST_V2.source.alias } });
    const mmm = await call<DashboardPresets>("ana", "GET", "/v1/dashboards:presets?alias=geo-mmm-prod", undefined, {}, "acme/growth");
    expect(mmm.data.suggestions?.[0]?.preset).toBe("mmm_planner");
    expect((await call("ana", "GET", "/v1/dashboards:presets")).status).toBe(422);
    expect((await call("ana", "GET", "/v1/dashboards:presets?alias=nope")).status).toBe(404);
    expect((await call("cleo", "GET", `/v1/dashboards:presets?alias=${FORECAST_V2.source.alias}`)).status).toBe(403);
  });

  it("create a layout 2 dashboard from a preset, check, publish and read its catalog", async () => {
    const spec = (await call<DashboardPresets>("ana", "GET", `/v1/dashboards:presets?alias=${REGRESSION_V2.source.alias}`)).data.spec!;
    const made = await call<Dashboard>("ana", "POST", "/v1/dashboards", { name: "new-regression", spec: { ...spec, limits: { exact_runs_per_day: 1, batch_runs_per_day: 1 } } });
    expect(made.status).toBe(201);
    const id = made.data.id;
    const cat = await call<SourceCatalog>("ana", "GET", `/v1/dashboards/${id}/sources`);
    expect(cat.data.outputs?.map((o) => o.name)).toContain("y_hat");
    const bad = { ...made.data.draft_spec, panels: [...(made.data.draft_spec as DashboardSpecV2).panels, { kind: "kpi", id: "nope", title: "Nope", source: { kind: "output", variable: "nothing" } }] };
    const put = await call<Dashboard>("ana", "PUT", `/v1/dashboards/${id}/draft`, { spec: bad }, { "If-Match": made.data.draft_etag });
    const v = await call<{ ok: boolean; issues: { pointer: string }[] }>("ana", "POST", `/v1/dashboards/${id}:validate`);
    expect(v.data.ok).toBe(false);
    expect(v.data.issues.map((i) => i.pointer)).toContain(`/panels/${bad.panels.length - 1}/source/variable`);
    const layout1 = { ...REGRESSION_V2, layout_version: 1 };
    expect((await call<{ code: string }>("ana", "PUT", `/v1/dashboards/${id}/draft`, { spec: layout1 }, { "If-Match": put.data.draft_etag })).data.code).toBe("DASHBOARD_LAYOUT_RETIRED");
    await call("ana", "PUT", `/v1/dashboards/${id}/draft`, { spec: made.data.draft_spec }, { "If-Match": put.data.draft_etag });
    expect((await call<{ ok: boolean }>("ana", "POST", `/v1/dashboards/${id}:validate`)).data.ok).toBe(true);
    const pub = await call<Dashboard>("ana", "POST", `/v1/dashboards/${id}:publish`);
    expect(pub.data.status).toBe("published");
    const first = await settled("ana", id, 1);
    expect(first.snapshot).toMatchObject({ schema_version: 2, preset: "regression" });
    // the daily cap counts every run an input spends
    const one = await call<Readout>("ana", "POST", `/v1/dashboards/${id}/predictions`, { rows: [{ price: 20 }] }, { "Idempotency-Key": "cap-1" });
    expect(one.status).toBe(202);
    const two = await call<{ code: string }>("ana", "POST", `/v1/dashboards/${id}/predictions`, { rows: [{ price: 21 }] }, { "Idempotency-Key": "cap-2" });
    expect(two).toMatchObject({ status: 429, data: { code: "DASHBOARD_RUN_LIMIT" } });
    expect(two.headers.get("Retry-After")).toBe("60");
  });

  it("upgrade a layout 1 draft on request (If-Match), keeping its words and binding", async () => {
    const d = (await call<Dashboard>("ana", "GET", `/v1/dashboards/${FORECAST_DASHBOARD_ID}`, undefined, {}, "acme/growth")).data;
    expect((await call("ana", "POST", `/v1/dashboards/${FORECAST_DASHBOARD_ID}:upgrade`, undefined, {}, "acme/growth")).status).toBe(428);
    const up = await call<DashboardUpgrade>("ana", "POST", `/v1/dashboards/${FORECAST_DASHBOARD_ID}:upgrade`, undefined, { "If-Match": d.draft_etag }, "acme/growth");
    expect(up.status).toBe(200);
    const spec = up.data.dashboard.draft_spec as DashboardSpecV2;
    expect(spec).toMatchObject({ layout_version: 2, preset: "forecast", title: "Monthly sales forecast", source: { alias: "sales-forecast" } });
    expect(spec.subject).toMatchObject({ variable: "y_hat", label: "Sales" });
    expect(spec.limits?.exact_runs_per_day).toBe(20);
    expect((up.data.notes ?? []).join(" ")).toContain("placeholder");
    expect(up.data.dashboard.draft_etag).not.toBe(d.draft_etag);
    // the published revision (what clients see) is still layout 1
    expect(up.data.dashboard.published_revision?.spec.layout_version).toBe(1);
    expect((await call("ana", "POST", `/v1/dashboards/${FORECAST_DASHBOARD_ID}:upgrade`, undefined, { "If-Match": up.data.dashboard.draft_etag }, "acme/growth")).status).toBe(409);
    expect((await call<{ ok: boolean }>("ana", "POST", `/v1/dashboards/${FORECAST_DASHBOARD_ID}:validate`, undefined, {}, "acme/growth")).data.ok).toBe(true);
  });
});

describe("layout 2 inputs", () => {
  it("try a case: refuse values out of range (pointers per row), then score and keep it", async () => {
    const out = await call<{ code: string; errors: { pointer: string }[] }>("ana", "POST", `${D("regression")}/predictions`, { rows: [{ price: 100 }, { colour: "red" }] }, { "Idempotency-Key": "t-0" });
    expect(out.status).toBe(422);
    expect(out.data.code).toBe("DASHBOARD_INPUT_INVALID");
    expect(out.data.errors.map((e) => e.pointer)).toEqual(["/rows/0/price", "/rows/1/colour"]);
    const off = await call<{ errors: { message: string }[] }>("ana", "POST", `${D("regression")}/predictions`, { rows: [{ price: 18.2 }] }, { "Idempotency-Key": "t-0b" });
    expect(off.data.errors[0]?.message).toContain("multiple of 0.5");
    const sent = await call<Readout>("ana", "POST", `${D("regression")}/predictions`, { rows: [{ price: 12, store: "S1" }, { price: 30, promo: 1 }], label: "Two prices" }, { "Idempotency-Key": "t-1" });
    expect(sent.status).toBe(202);
    expect(sent.data).toMatchObject({ kind: "prediction", visibility: "private", title: "Two prices" });
    const again = await call<Readout>("ana", "POST", `${D("regression")}/predictions`, { rows: [{ price: 12 }] }, { "Idempotency-Key": "t-1" });
    expect(again.data.seq).toBe(sent.data.seq);
    const r = await settled("ana", V2_DASHBOARD_IDS.regression, sent.data.seq);
    const rows = r.snapshot?.prediction?.rows ?? [];
    expect(rows).toHaveLength(2);
    const a = rows[0]!.outputs!.y_hat!;
    const b = rows[1]!.outputs!.y_hat!;
    expect(a.low! < a.centre! && a.centre! < a.high!).toBe(true);
    expect(a.centre!).toBeGreaterThan(b.centre! / 1.3); // a lower price sells more, a promotion lifts
    expect(r.snapshot?.lead).toMatch(/^For the case you set, the model predicts [\d.,]+ units sold, likely between/);
    // a client never sees another caller's tried case
    expect((await call("cleo", "GET", `${D("regression")}/readouts/${sent.data.seq}`)).status).toBe(404);
    const kept = await call<ReadoutHeader>("ana", "POST", `${D("regression")}/readouts/${sent.data.seq}:keep`);
    expect(kept.data.visibility).toBe("pending_review");
    const pub = await call<ReadoutHeader>("ana", "POST", `${D("regression")}/readouts/${sent.data.seq}:publish`);
    expect(pub.data.visibility).toBe("published");
    expect((await call("cleo", "GET", `${D("regression")}/readouts/${sent.data.seq}`)).status).toBe(200);
    expect((await call("ana", "POST", `${D("regression")}/readouts/1:keep`)).status).toBe(409);
  });

  it("score a file: columns and ranges checked, summary of centres, CSV for its owner and editors", async () => {
    const t = (await call<BatchTemplate>("ana", "GET", `${D("classifier")}/batch/template`)).data;
    expect(t.columns.map((c) => c.name)).toEqual(["store", "time", "price", "promo"]);
    expect(t.example_csv.split("\n")[0]).toBe("store,time,price,promo");
    const file = (ids: string[], extra: Record<string, { dims: string[]; data: unknown[] }> = {}) => ({
      inputs: {
        coords: { store: { dims: ["store"], data: ids } },
        data_vars: {
          time: { dims: ["store"], data: ids.map(() => "2024-10-13") },
          price: { dims: ["store"], data: ids.map((_, i) => 10 + i) },
          promo: { dims: ["store"], data: ids.map((_, i) => i % 2) },
          ...extra,
        },
      },
      label: "Planned weeks",
    });
    const unknown = await call<{ code: string; errors: { pointer: string }[] }>("ana", "POST", `${D("classifier")}/batch`, file(["a"], { colour: { dims: ["store"], data: ["red"] } }), { "Idempotency-Key": "b-0" });
    expect(unknown.data.errors.map((e) => e.pointer)).toContain("/inputs/data_vars/colour");
    const range = await call<{ errors: { pointer: string }[] }>("ana", "POST", `${D("classifier")}/batch`, file(["a", "b"], { price: { dims: ["store"], data: [10, 99] } }), { "Idempotency-Key": "b-1" });
    expect(range.data.errors[0]?.pointer).toBe("/inputs/data_vars/price/data/1");
    const big = await call<{ code: string }>("ana", "POST", `${D("classifier")}/batch`, file(Array.from({ length: 2001 }, (_, i) => `c${i}`)), { "Idempotency-Key": "b-2" });
    expect(big).toMatchObject({ status: 409, data: { code: "DASHBOARD_BATCH_TOO_LARGE" } });
    const sent = await call<Readout>("ana", "POST", `${D("classifier")}/batch`, file(["s0", "s3", "new-store", "=cmd"]), { "Idempotency-Key": "b-3" });
    expect(sent.status).toBe(202);
    const r = await settled("ana", V2_DASHBOARD_IDS.classifier, sent.data.seq);
    const batch = r.snapshot?.batch;
    expect(batch).toMatchObject({ n_rows: 4, n_scored: 4, outputs: ["y_hat"] });
    const st = batch!.stats!.y_hat!;
    expect(st.min! <= st.p50! && st.p50! <= st.max!).toBe(true);
    expect(batch!.share_positive).not.toBeNull();
    expect(batch!.histogram!.reduce((a, h) => a + h.count, 0)).toBe(4);
    const csv = await call<string>("ana", "GET", `${D("classifier")}/readouts/${sent.data.seq}/predictions.csv`);
    expect(csv.status).toBe(200);
    expect(csv.headers.get("content-type")).toContain("text/csv");
    const lines = csv.data.trim().split("\n");
    expect(lines[0]).toBe("store,time,price,promo,y_hat,y_hat_low,y_hat_high");
    expect(lines).toHaveLength(5);
    expect(lines[4]!.startsWith("'=cmd,")).toBe(true); // a formula cell is defused
    // a share link may download its own files only
    const link = await openLink(MOCK_SHARE_TOKENS.classifier);
    expect((await shared(link.session, "GET", `/v1/shared/readouts/${sent.data.seq}/predictions.csv`)).status).toBe(404);
    expect((await call("ana", "GET", `${D("classifier")}/readouts/1/predictions.csv`)).status).toBe(404);
  });

  it("send actuals of any family: per case, per period and the updated effect", async () => {
    const t = (await call<{ match: string; id_column: string; kpi_column: string }>("cleo", "GET", `${D("classifier")}/actuals/template`)).data;
    expect(t).toMatchObject({ match: "row", id_column: "store", kpi_column: "y" });
    const stores = ["s0", "s1", "s2", "s3", "s4"];
    const rowBody = {
      inputs: {
        coords: { store: { dims: ["store"], data: stores } },
        data_vars: { time: { dims: ["store"], data: stores.map(() => "2024-10-13") }, price: { dims: ["store"], data: [12, 14, 16, 30, 35] }, promo: { dims: ["store"], data: [1, 1, 0, 0, 0] }, y: { dims: ["store"], data: [1, 1, 0, 0, 0] } },
      },
    };
    const sent = await call<ReadoutHeader>("cleo", "POST", `${D("classifier")}/actuals`, rowBody, { "Idempotency-Key": "a-1" });
    expect(sent.status).toBe(202);
    const r = await settled("cleo", V2_DASHBOARD_IDS.classifier, sent.data.seq);
    expect(r.snapshot?.accuracy_detail).toMatchObject({ kind: "classification", n: 5 });
    expect(r.snapshot?.lead).toMatch(/^On the 5 cases you sent, the model was right \d+% of the time\.$/);
    const conf = r.snapshot?.panels?.find((b) => b.kind === "confusion");
    expect(conf?.kind === "confusion" && (conf.counts ?? []).flat().reduce<number>((a, b) => a + (b ?? 0), 0)).toBe(5);

    const ft = (await call<{ input_table: { horizon_dim: string }; kpi_column: string }>("cleo", "GET", `${D("forecast")}/actuals/template`)).data;
    const dim = ft.input_table.horizon_dim;
    const timeBody = { inputs: { coords: { [dim]: { dims: [dim], data: ["2020-01-01", "2020-02-01"] } }, data_vars: { [ft.kpi_column]: { dims: [dim], data: [1080, 1100] } } } };
    const fs = await call<ReadoutHeader>("cleo", "POST", `${D("forecast")}/actuals`, timeBody, { "Idempotency-Key": "a-2" });
    expect(fs.data.visibility).toBe("published");
    const fr = await settled("cleo", V2_DASHBOARD_IDS.forecast, fs.data.seq);
    expect(fr.snapshot?.accuracy_detail).toMatchObject({ kind: "error", n: 2 });
    expect(fr.snapshot?.accuracy_history?.map((h) => h.seq)).toEqual([3, 5, 7, fs.data.seq]);
    const none = await call<{ code: string }>("cleo", "POST", `${D("forecast")}/actuals`, { inputs: { coords: { [dim]: { dims: [dim], data: ["1999-01-01"] } }, data_vars: { [ft.kpi_column]: { dims: [dim], data: [5] } } } }, { "Idempotency-Key": "a-3" });
    expect(none.data.code).toBe("ACTUALS_INVALID");

    const ct = (await call<{ input_table: { horizon_dim: string }; kpi_column: string }>("cleo", "GET", `${D("causal")}/actuals/template`)).data;
    const cdim = ct.input_table.horizon_dim;
    const effectBody = { inputs: { coords: { [cdim]: { dims: [cdim], data: ["2024-03-25", "2024-04-01", "2024-04-08", "2024-04-15"] } }, data_vars: { [ct.kpi_column]: { dims: [cdim], data: [105, 106, 104, 107] } } } };
    const cs = await call<ReadoutHeader>("cleo", "POST", `${D("causal")}/actuals`, effectBody, { "Idempotency-Key": "a-4" });
    const cr = await settled("cleo", V2_DASHBOARD_IDS.causal, cs.data.seq);
    const eff = cr.snapshot?.accuracy_detail;
    expect(eff?.kind).toBe("effect");
    if (eff?.kind === "effect") {
      expect(eff.total).toBeCloseTo(422 - 406.891, 3);
      expect(eff.low! < eff.total! && eff.total! < eff.high!).toBe(true);
    }
  });
});

describe("layout 2 share links", () => {
  it("try a case, score a file and download it through a link (no keep route)", async () => {
    const s = await openLink(MOCK_SHARE_TOKENS.classifier);
    expect(s.dashboard).toMatchObject({ layout_version: 2, preset: "classifier" });
    expect(s.dashboard.inputs).toMatchObject({ scenario_rows: true, batch: true, actuals: true });
    const p = await shared<Readout>(s.session, "POST", "/v1/shared/predictions", { rows: [{ price: 20, promo: 1 }] }, { "Idempotency-Key": "s-1", Prefer: "wait=5" });
    expect(p.status).toBe(200);
    expect(p.data.snapshot?.prediction?.rows?.[0]?.outputs?.y_hat?.centre).toBeGreaterThan(0);
    expect(p.data.snapshot?.lead).toMatch(/% chance of a positive result for this case\.$/);
    const t = await shared<BatchTemplate>(s.session, "GET", "/v1/shared/batch/template");
    expect(t.data.id_column).toBe("store");
    const b = await shared<Readout>(s.session, "POST", "/v1/shared/batch", { inputs: { coords: { store: { dims: ["store"], data: ["x1"] } }, data_vars: { time: { dims: ["store"], data: ["2024-10-13"] }, price: { dims: ["store"], data: [18] }, promo: { dims: ["store"], data: [0] } } } }, { "Idempotency-Key": "s-2" });
    expect(b.status).toBe(202);
    await wait(60);
    const csv = await shared<string>(s.session, "GET", `/v1/shared/readouts/${b.data.seq}/predictions.csv`);
    expect(csv.status).toBe(200);
    expect(csv.data.split("\n")[1]?.startsWith("x1,")).toBe(true);
    // the link sees its own tried cases only, and cannot keep them
    expect((await shared<{ code: string }>(s.session, "POST", `/v1/shared/readouts/${p.data.seq}:keep`)).status).toBe(404);
    const list = (await shared<ReadoutList>(s.session, "GET", "/v1/shared/readouts")).data.items ?? [];
    expect(list.some((r) => r.seq === p.data.seq)).toBe(true);
    expect(list.some((r) => r.kind === "prediction" && r.seq !== p.data.seq && r.visibility === "private")).toBe(false);
    // a share request carries no project or CSRF header
    expect((await shared(s.session, "GET", "/v1/shared/batch/template", undefined, { "X-Mapi-Project": V2_PROJECT })).status).toBe(400);
  });

  it("serve the pictures of the revision; no image scoring through a link", async () => {
    const s = await openLink(MOCK_SHARE_TOKENS.image);
    const pic = s.dashboard.latest_readout?.snapshot?.panels?.find((x) => x.kind === "picture");
    expect(pic?.kind).toBe("picture");
    if (pic?.kind !== "picture") return;
    const png = await fetch(`${env.apiBase}/v1/shared/assets/${pic.asset_id}`, { headers: { "X-Mapi-Share": s.session } });
    expect(png.status).toBe(200);
    expect(png.headers.get("content-type")).toBe("image/png");
    expect(png.headers.get("x-content-type-options")).toBe("nosniff");
    expect([...new Uint8Array(await png.arrayBuffer()).slice(1, 4)]).toEqual([0x50, 0x4e, 0x47]);
    expect((await shared(s.session, "GET", "/v1/shared/assets/dsa_01jaf3w1nope2nope3x4y5z6pp")).status).toBe(404);
    expect((await call("ana", "GET", `${D("image")}/assets/${pic.asset_id}`)).status).toBe(200);
    expect((await shared<{ code: string }>(s.session, "POST", "/v1/shared/batch", { inputs: { coords: { image: { dims: ["image"], data: ["a"] } }, data_vars: {} } })).data.code).toBe("DASHBOARD_INPUT_DISABLED");
  });

  it("open the forecast and upgraded MMM links", async () => {
    const f = await openLink(MOCK_SHARE_TOKENS.forecastV2);
    expect(f.dashboard.sections).toEqual(["overview", "try", "accuracy", "history"]);
    expect(f.dashboard.scenario_form?.horizon).toMatchObject({ dim: "t", max: 12 });
    const p = await shared<{ errors: { pointer: string }[] }>(f.session, "POST", "/v1/shared/predictions", { rows: [{ horizon: 40 }] }, { "Idempotency-Key": "f-1" });
    expect(p.data.errors[0]?.pointer).toBe("/rows/0/horizon");
    const m = await openLink(MOCK_SHARE_TOKENS.mmmUpgraded);
    expect(m.dashboard.upgrade_note).toBeTruthy();
    expect(m.dashboard.inputs).toMatchObject({ whatif: false, actuals: false });
    expect(CLASSIFIER_V2.layout_version).toBe(2);
  });
});

/**
 * Client users reach the v0.17 inputs once `mocks/tenancy.ts` lists them among the client
 * routes (`mapi_spec.api.CLIENT_OPERATIONS`); until then the tenancy guard answers 403.
 */
describe.skipIf(!isClientRoute("POST", `/v1/dashboards/${V2_DASHBOARD_IDS.classifier}/predictions`))("layout 2 inputs of a client user", () => {
  it("try, keep, score a file, download it and see the pictures", async () => {
    const p = await call<Readout>("cleo", "POST", `${D("classifier")}/predictions`, { rows: [{ price: 16 }] }, { "Idempotency-Key": "c-1" });
    expect(p.status).toBe(202);
    await settled("cleo", V2_DASHBOARD_IDS.classifier, p.data.seq);
    expect((await call<ReadoutHeader>("cleo", "POST", `${D("classifier")}/readouts/${p.data.seq}:keep`)).data.visibility).toBe("pending_review");
    const theirs = await call<Readout>("ana", "POST", `${D("classifier")}/predictions`, { rows: [{ price: 17 }] }, { "Idempotency-Key": "c-2" });
    expect((await call("cleo", "POST", `${D("classifier")}/readouts/${theirs.data.seq}:keep`)).status).toBe(404);
    const t = (await call<BatchTemplate>("cleo", "GET", `${D("classifier")}/batch/template`)).data;
    expect(t.id_column).toBe("store");
    const b = await call<Readout>("cleo", "POST", `${D("classifier")}/batch`, { inputs: { coords: { store: { dims: ["store"], data: ["q1"] } }, data_vars: { time: { dims: ["store"], data: ["2024-10-13"] }, price: { dims: ["store"], data: [18] }, promo: { dims: ["store"], data: [0] } } } }, { "Idempotency-Key": "c-3" });
    await settled("cleo", V2_DASHBOARD_IDS.classifier, b.data.seq);
    expect((await call<string>("cleo", "GET", `${D("classifier")}/readouts/${b.data.seq}/predictions.csv`)).status).toBe(200);
    expect((await call<{ code: string }>("cleo", "GET", `${D("image")}/batch/template`)).data.code).toBe("DASHBOARD_INPUT_DISABLED");
    const pic = (await call<ClientDashboard>("cleo", "GET", D("image"))).data.latest_readout?.snapshot?.panels?.find((x) => x.kind === "picture");
    expect(pic?.kind === "picture" && (await call("cleo", "GET", `${D("image")}/assets/${pic.asset_id}`)).status).toBe(200);
  });
});
