import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { env } from "@/env";
import { createHandlers } from "@/mocks/handlers";
import { MOCK_SESSION_KEY, mockCsrfToken, PERSONAS } from "@/mocks/tenancy";
import { useAuth } from "@/stores/auth";

import { dk, memberCacheKey, shareCacheKey } from "./keys";
import { effectiveInputs, kpiOfSubject, memberClient, problemOf, shareClient, toClientDashboard, visibleSections } from "./client";
import {
  asLayout1,
  GLOSSARY_KEYS,
  GLOSSARY_KEYS_V2,
  hasPanels,
  isLayout2,
  isV2Spec,
  panelBlock,
  panelMeta,
  readoutLayout,
  SHARE_INPUTS,
  type Dashboard,
  type DashboardSpec,
  type DashboardSpecV2,
  type Readout,
  type ReadoutSnapshot,
  type ShareSessionHandle,
} from "./types";

const API = env.apiBase;
const server = setupServer(...createHandlers());
const seen: Request[] = [];

beforeAll(() => {
  server.listen({ onUnhandledRequest: "bypass" });
  server.events.on("request:start", ({ request }) => {
    seen.push(request.clone());
  });
});
beforeEach(() => {
  seen.length = 0;
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

const SESSION = `mps_${"a".repeat(20)}.${"b".repeat(43)}`;

function handle(): ShareSessionHandle {
  return { current: () => SESSION, expired: () => false, refresh: () => Promise.resolve() };
}

function readout(seq: number, patch: Partial<Readout> = {}): Readout {
  return {
    seq,
    kind: "prediction",
    trigger: "scenario_rows",
    visibility: "private",
    status: "queued",
    title: "Tried case",
    model_version_id: "mv_1",
    model_ref: "model://x@v1",
    created_at: "2026-10-21T09:00:00Z",
    snapshot: null,
    ...patch,
  };
}

const TEMPLATE = {
  id_column: "case_id",
  max_rows: 2000,
  example_csv: "case_id,age\nc1,40\n",
  columns: [
    { name: "case_id", role: "id" as const, required: true },
    { name: "age", role: "driver" as const, type: "number" as const, min: 18, max: 90 },
  ],
};

/** The v0.17 routes, under the share and the member paths alike (`prefix`). */
function v2Routes(prefix: string) {
  return [
    http.post(`${API}${prefix}/predictions`, async ({ request }) => {
      const body = (await request.json()) as { rows: Record<string, unknown>[] };
      const age = body.rows[0]?.age;
      if (typeof age === "number" && age > 90) {
        return HttpResponse.json(
          { type: "about:blank", title: "Invalid input", status: 422, code: "DASHBOARD_INPUT_INVALID", errors: [{ pointer: "/rows/0/age", message: "at most 90" }] },
          { status: 422, headers: { "Content-Type": "application/problem+json" } },
        );
      }
      return HttpResponse.json(readout(7), { status: 202 });
    }),
    http.get(`${API}${prefix}/batch/template`, () => HttpResponse.json(TEMPLATE)),
    http.post(`${API}${prefix}/batch`, () => HttpResponse.json(readout(8, { kind: "batch", trigger: "batch" }), { status: 202 })),
    http.get(`${API}${prefix}/readouts/:seq/predictions.csv`, ({ params }) =>
      params.seq === "8"
        ? new HttpResponse("case_id,p\nc1,0.4\n'=1+1,0.5\n", { headers: { "Content-Type": "text/csv" } })
        : HttpResponse.json(
            { type: "about:blank", title: "Gone", status: 410, code: "DASHBOARD_DOWNLOAD_GONE" },
            { status: 410, headers: { "Content-Type": "application/problem+json" } },
          ),
    ),
    http.get(`${API}${prefix}/assets/:asset`, () => new HttpResponse(new Uint8Array([0x89, 0x50, 0x4e, 0x47]), { headers: { "Content-Type": "image/png" } })),
    http.post(`${API}${prefix}/readouts/:seq\\:keep`, ({ params }) => HttpResponse.json({ ...readout(Number(params.seq)), visibility: "pending_review", status: "ready" })),
  ];
}

describe("shareClient (layout 2)", () => {
  beforeEach(() => server.use(...v2Routes("/v1/shared")));

  it("tries a case, scores a file and downloads with the session header only", async () => {
    // A signed-in analyst in the same browser: the share client still sends none of it.
    localStorage.setItem(MOCK_SESSION_KEY, "ana");
    useAuth.setState({ mode: "session", csrf: mockCsrfToken(PERSONAS.ana!) });
    const spy = vi.spyOn(globalThis, "fetch");
    const c = shareClient(handle(), "dsh_1");

    const r = await c.predict({ rows: [{ age: 40 }], label: "Case" }, "k-1", { waitSeconds: 10 });
    expect(r).toMatchObject({ seq: 7, kind: "prediction", visibility: "private" });
    expect((await c.batchTemplate()).id_column).toBe("case_id");
    const b = await c.submitBatch({ inputs: { coords: { case_id: { dims: ["case_id"], data: ["c1"] } }, data_vars: { age: { dims: ["case_id"], data: [40] } } } }, "k-2");
    expect(b.kind).toBe("batch");
    const csv = await c.downloadPredictions(8);
    expect(csv.type).toMatch(/^text\/csv/);
    expect(await csv.text()).toContain("'=1+1");
    const png = await c.asset("dsa_01jaf3e1f2g3h4j5k6m7n8p9q0");
    expect(new Uint8Array(await png.arrayBuffer())[1]).toBe(0x50);
    expect(c.keep).toBeNull();

    for (const [input, init] of spy.mock.calls) {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      expect(url).toMatch(/\/v1\/shared\//);
      expect(init?.credentials).toBe("omit");
      expect(init?.referrerPolicy).toBe("no-referrer");
      const headers = new Headers(init?.headers);
      expect(headers.get("X-Mapi-Share")).toBe(SESSION);
      expect(headers.has("X-CSRF-Token")).toBe(false);
      expect(headers.has("X-Mapi-Project")).toBe(false);
      expect(url).not.toContain(SESSION);
    }
    const sent = seen.filter((x) => new URL(x.url).pathname.startsWith("/v1/shared/"));
    expect(sent.find((x) => x.url.endsWith("/predictions"))?.headers.get("Prefer")).toBe("wait=10");
    expect(sent.find((x) => x.url.endsWith("/predictions"))?.headers.get("Idempotency-Key")).toBe("k-1");
    expect(sent.find((x) => x.url.endsWith("predictions.csv"))?.headers.get("Accept")).toMatch(/^text\/csv/);
    spy.mockRestore();
    useAuth.setState({ mode: "unknown", csrf: null });
  });

  it("reports an out-of-range value with its pointer and an expired download as 410", async () => {
    const c = shareClient(handle());
    const bad = await c.predict({ rows: [{ age: 120 }] }, "k-3").catch((e: unknown) => e);
    expect(problemOf(bad)).toMatchObject({ status: 422, code: "DASHBOARD_INPUT_INVALID" });
    const gone = await c.downloadPredictions(9).catch((e: unknown) => e);
    expect(problemOf(gone)).toMatchObject({ status: 410, code: "DASHBOARD_DOWNLOAD_GONE" });
  });
});

describe("memberClient (layout 2)", () => {
  beforeEach(() => {
    server.use(...v2Routes("/v1/dashboards/dsh_1"));
    localStorage.setItem(MOCK_SESSION_KEY, "cleo");
    useAuth.setState({ mode: "session", csrf: mockCsrfToken(PERSONAS.cleo!) });
  });
  afterEach(() => useAuth.setState({ mode: "unknown", csrf: null }));

  it("tries a case, keeps it and downloads in the project", async () => {
    const c = memberClient("acme/growth", "dsh_1", { kind: "client" });
    const r = await c.predict({ rows: [{ age: 40 }] }, "m-1");
    expect(r.seq).toBe(7);
    const kept = await c.keep!(7);
    expect(kept).toMatchObject({ seq: 7, visibility: "pending_review" });
    expect(await (await c.downloadPredictions(8)).text()).toContain("case_id");
    expect((await c.asset("dsa_01jaf3e1f2g3h4j5k6m7n8p9q0")).size).toBe(4);
    expect((await c.batchTemplate()).max_rows).toBe(2000);
    const gone = await c.downloadPredictions(9).catch((e: unknown) => e);
    expect(problemOf(gone)).toMatchObject({ status: 410, code: "DASHBOARD_DOWNLOAD_GONE" });
    const sent = seen.filter((x) => new URL(x.url).pathname.startsWith("/v1/dashboards/dsh_1"));
    expect(sent.map((x) => new URL(x.url).pathname)).toContain("/v1/dashboards/dsh_1/readouts/7:keep");
    for (const x of sent) expect(x.headers.get("X-Mapi-Project")).toBe("acme/growth");
  });
});

// ------------------------------------------------------------------------------ pure helpers

const V2: DashboardSpecV2 = {
  layout_version: 2,
  preset: "classifier",
  title: "Churn",
  source: { kind: "endpoint", alias: "churn" },
  subject: { variable: "churn", label: "Chance of leaving", unit: "probability", decimals: 6, positive_label: "yes" },
  panels: [
    { kind: "kpi", id: "p_yes", title: "Chance of yes", source: { kind: "output", variable: "churn" } },
    { kind: "chart", id: "bars", title: "Classes", intent: "probability_bars", source: { kind: "output", variable: "churn" } },
    { kind: "recipe", id: "cal", title: "Calibration", recipe: "core.calibration" },
    { kind: "recipe", id: "curves", title: "Training", recipe: "ml.training_curves" },
  ],
  scenario_rows: {
    enabled: true,
    max_rows: 5,
    outputs: ["churn"],
    inputs: [
      { name: "case_id", label: "Case", bind: { var: "case_id" }, type: "number", default: 1 },
      { name: "age", label: "Age", bind: { var: "age" }, type: "number", min: 18, max: 90, default: 40 },
    ],
  },
  actuals: { enabled: true, match: "row", id_column: "case_id" },
  batch: { enabled: true, outputs: ["churn"], id_column: "case_id", max_rows: 2000 },
};

const V1 = {
  title: "T",
  source: { kind: "endpoint", alias: "a" },
  kpi: { variable: "y", label: "Y", unit: "count" },
  channels: [{ name: "tv", label: "TV" }],
  tabs: ["this_period", "scenarios", "update", "history"],
  whatif: { enabled: true, exact: true },
  actuals: { enabled: true, publish: "review", allow_refit_requests: true },
} as DashboardSpec;

describe("layout 2 helpers", () => {
  it("tells the layouts apart", () => {
    expect(isLayout2(V2)).toBe(true);
    expect(isV2Spec(V1)).toBe(false);
    expect(asLayout1(V2)).toBeNull();
    expect(asLayout1(V1)).toBe(V1);
  });

  it("keeps the layout 1 inputs exactly as v0.16 did", () => {
    expect(effectiveInputs(V1)).toEqual({ whatif: true, exact: true, actuals: true, actuals_mode: "compared", refit_requests: true });
  });

  it("narrows layout 2 inputs like access.effective_inputs", () => {
    expect(effectiveInputs(V2)).toEqual({
      whatif: false,
      exact: false,
      actuals: true,
      actuals_mode: "compared",
      refit_requests: false,
      scenario_rows: true,
      batch: true,
      accuracy_kind: "classification",
    });
    const link = effectiveInputs(V2, null, ["scenario_rows"]);
    expect(link).toMatchObject({ scenario_rows: true, batch: false, actuals: false, accuracy_kind: null });
    expect(visibleSections(link)).toEqual(["overview", "try", "history"]);
    expect(visibleSections(effectiveInputs(V2))).toEqual(["overview", "try", "accuracy", "batch", "history"]);

    // image batch scoring: members only (never a client user, never a share link)
    const image = { ...V2, preset: "image" } as DashboardSpecV2;
    expect(effectiveInputs(image).batch).toBe(true);
    expect(effectiveInputs(image, null, null, { client: true }).batch).toBe(false);
    expect(effectiveInputs(image, null, [...SHARE_INPUTS]).batch).toBe(false);
    expect(effectiveInputs({ ...V2, preset: "causal_impact" as const }).accuracy_kind).toBe("effect");
    expect(effectiveInputs({ ...V2, preset: "forecast" as const }).accuracy_kind).toBe("error");
  });

  it("builds the client view of a layout 2 draft (an upgraded dashboard no longer breaks the preview)", () => {
    const d = { id: "dsh_2", name: "n", status: "draft", draft_spec: V2, draft_etag: "e", created_by: "u", created_at: "t", updated_at: "t" } as Dashboard;
    const v = toClientDashboard(d, null);
    expect(v).toMatchObject({
      id: "dsh_2",
      title: "Churn",
      layout_version: 2,
      preset: "classifier",
      period_label: "period",
      channels: [],
      tiles: [],
      tabs: [],
      sections: ["overview", "try", "accuracy", "batch", "history"],
      upgrade_note: null,
    });
    expect(v.kpi).toEqual({ variable: "churn", label: "Chance of leaving", unit: "percent", currency: null, decimals: 4, value_per_unit: null });
    expect(v.panels_meta?.map((p) => [p.id, p.intent])).toEqual([
      ["p_yes", null],
      ["bars", "probability_bars"],
      ["cal", "calibration"],
      ["curves", null],
    ]);
    expect(v.scenario_form?.inputs?.map((i) => i.name)).toEqual(["case_id", "age"]);
    expect(v.batch_form).toEqual({ max_rows: 2000, id_column: "case_id", columns: ["case_id", "age"], outputs: ["churn"] });
  });

  it("previews an image dashboard without Score a file, as its clients see it", () => {
    const image = { ...V2, preset: "image" } as DashboardSpecV2;
    const d = { id: "dsh_i", name: "n", status: "draft", draft_spec: image, draft_etag: "e", created_by: "u", created_at: "t", updated_at: "t" } as Dashboard;
    const v = toClientDashboard(d, null);
    expect(v.inputs?.batch).toBe(false);
    expect(v.batch_form).toBeNull();
    expect(v.sections).not.toContain("batch");
  });

  it("gives an MMM planner its layout 1 tiles and tabs", () => {
    const mmm = {
      ...V2,
      preset: "mmm_planner",
      subject: { variable: "revenue", label: "Revenue", unit: "currency", currency: "EUR" },
      mmm: { channels: [{ name: "tv", label: "TV" }], whatif: { enabled: true, exact: false } },
      scenario_rows: { enabled: false },
      batch: null,
      actuals: { enabled: false },
    } as DashboardSpecV2;
    const v = toClientDashboard({ id: "d", name: "n", status: "draft", draft_spec: mmm, draft_etag: "e", created_by: "u", created_at: "t", updated_at: "t" }, null);
    expect(v.tiles).toEqual(["expected", "vs_expected", "return_on_media", "in_range_spend"]);
    expect(v.tabs).toEqual(["this_period", "trends", "scenarios", "history"]);
    expect(v.channels).toEqual([{ name: "tv", label: "TV" }]);
    expect(v.sections).toEqual(["overview", "history"]);
  });

  it("maps a subject to the layout 1 KPI", () => {
    expect(kpiOfSubject({ variable: "y", label: "Y", unit: "rate" }).unit).toBe("ratio");
    expect(kpiOfSubject({ variable: "y", label: "Y", unit: "label" }).unit).toBe("count");
  });

  it("picks each readout's renderer by its own snapshot first", () => {
    const v2snap = { lead: "x", schema_version: 2, panels: [{ kind: "note", panel_id: "n", title: "N", text: "hi" }] } as ReadoutSnapshot;
    const v1snap = { lead: "x", tiles: [] } as ReadoutSnapshot;
    expect(hasPanels(v2snap)).toBe(true);
    expect(hasPanels(v1snap)).toBe(false);
    expect(hasPanels(null)).toBe(false);
    expect(readoutLayout({ layout_version: 2 }, { snapshot: v1snap })).toBe(1);
    expect(readoutLayout({ layout_version: 1 }, { snapshot: v2snap })).toBe(2);
    expect(readoutLayout({ layout_version: 2 }, { snapshot: null })).toBe(2);
    expect(readoutLayout({}, null)).toBe(1);
    expect(panelBlock(v2snap, "n")?.kind).toBe("note");
    expect(panelBlock(v2snap, "zz")).toBeNull();
  });

  it("mirrors panel_meta and the v0.17 glossary", () => {
    expect(panelMeta({ kind: "note", id: "n", title: "N", text: "t" })).toEqual({ id: "n", kind: "note", title: "N", caption: null, intent: null });
    expect(GLOSSARY_KEYS_V2.slice(0, GLOSSARY_KEYS.length)).toEqual([...GLOSSARY_KEYS]);
    expect(GLOSSARY_KEYS_V2).toContain("expected_count");
  });

  it("keys the v0.17 reads under the client's view and the project", () => {
    expect(dk.viewAsset(shareCacheKey("dsh_1"), "dsa_x")).toEqual(["share", "dsh_1", "view", "asset", "dsa_x"]);
    expect(dk.viewBatchTemplate(memberCacheKey("acme/growth", "dsh_1", "client")).slice(0, 4)).toEqual(dk.one("acme/growth", "dsh_1"));
    expect(dk.sources("acme/growth", "dsh_1")).toEqual([...dk.one("acme/growth", "dsh_1"), "sources"]);
    expect(dk.presets("acme/growth", { alias: "a" }).slice(0, 3)).toEqual(dk.all("acme/growth"));
  });
});
