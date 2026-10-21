import { setupServer } from "msw/node";
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";

import type { Dashboard, DashboardRefitRequest, DashboardShareCreated, DashboardSummary, Readout, ReadoutHeader, ReadoutList } from "@/api/types";
import { env } from "@/env";
import { layout1Spec } from "@/features/dashboards/types";

import { DRAFT_DASHBOARD_ID, FORECAST_DASHBOARD_ID, MMM_DASHBOARD_ID, whatIfOf, zoneOf } from "./dashboards";
import { createHandlers } from "./handlers";
import { MOCK_SESSION_KEY, mockCsrfToken, PERSONAS } from "./tenancy";

const server = setupServer(...createHandlers());
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterAll(() => server.close());
beforeEach(() => localStorage.setItem("mapi.mock.readoutMs", "30"));

type Who = keyof typeof PERSONAS;

async function call<T>(who: Who, method: string, path: string, body?: unknown, headers: Record<string, string> = {}): Promise<{ status: number; data: T }> {
  localStorage.setItem(MOCK_SESSION_KEY, who);
  const res = await fetch(`${env.apiBase}${path}`, {
    method,
    headers: { "X-Mapi-Project": "acme/growth", "X-CSRF-Token": mockCsrfToken(PERSONAS[who]!), ...(body !== undefined ? { "Content-Type": "application/json" } : {}), ...headers },
    body: body === undefined ? null : JSON.stringify(body),
  });
  const text = await res.text();
  return { status: res.status, data: (text ? JSON.parse(text) : null) as T };
}

const wait = (ms: number) => new Promise((r) => setTimeout(r, ms));

describe("client users (D4)", () => {
  it("see only the published dashboards granted to them", async () => {
    const list = await call<{ items: DashboardSummary[] }>("cleo", "GET", "/v1/dashboards");
    expect(list.data.items.map((d) => d.id).sort()).toEqual([FORECAST_DASHBOARD_ID, MMM_DASHBOARD_ID].sort());
    expect((await call("cleo", "GET", `/v1/dashboards/${DRAFT_DASHBOARD_ID}`)).status).toBe(404);
    const d = await call<Record<string, unknown>>("cleo", "GET", `/v1/dashboards/${MMM_DASHBOARD_ID}`);
    expect(d.data).not.toHaveProperty("draft_spec");
    expect(d.data).toHaveProperty("inputs");
  });

  it("cannot reach anything else", async () => {
    for (const [m, p] of [
      ["GET", "/v1/runs"],
      ["GET", "/v1/datasets"],
      ["GET", `/v1/dashboards/${MMM_DASHBOARD_ID}/grants`],
      ["GET", `/v1/dashboards/${MMM_DASHBOARD_ID}/refit-requests`],
      ["POST", `/v1/dashboards/${MMM_DASHBOARD_ID}/refit-requests/rfr_01jaf3w1refit2open3x4y5z6:approve`],
      ["POST", `/v1/dashboards/${MMM_DASHBOARD_ID}:publish`],
    ] as const) {
      expect((await call("cleo", m, p, m === "POST" ? {} : undefined)).status, `${m} ${p}`).toBe(403);
    }
    expect((await call("cleo", "GET", "/v1/me")).status).toBe(200);
  });

  it("are listed with the client role in their projects", async () => {
    const me = await call<{ tenants: { projects: { role: string }[] }[] }>("cleo", "GET", "/v1/me");
    expect(me.data.tenants[0]?.projects.map((p) => p.role)).toEqual(["client", "client"]);
  });
});

describe("analysts", () => {
  it("create, edit (If-Match), validate and publish a dashboard (a runner publishes; it runs as them)", async () => {
    const draft = (await call<Dashboard>("ana", "GET", `/v1/dashboards/${DRAFT_DASHBOARD_ID}`)).data;
    expect(draft.status).toBe("draft");
    const created = await call<Dashboard>("ana", "POST", "/v1/dashboards", { name: "copy-of-promo", spec: draft.draft_spec });
    expect(created.status).toBe(201);
    const id = created.data.id;
    expect((await call("ana", "POST", "/v1/dashboards", { name: "copy-of-promo", spec: draft.draft_spec })).status).toBe(409);
    expect((await call("vic", "POST", "/v1/dashboards", { name: "x", spec: draft.draft_spec })).status).toBe(403);

    const spec = { ...created.data.draft_spec, title: "Changed" };
    expect((await call("ana", "PUT", `/v1/dashboards/${id}/draft`, { spec })).status).toBe(428);
    expect((await call<{ code: string }>("ana", "PUT", `/v1/dashboards/${id}/draft`, { spec }, { "If-Match": '"stale"' })).data.code).toBe("DASHBOARD_SPEC_STALE");
    const put = await call<Dashboard>("ana", "PUT", `/v1/dashboards/${id}/draft`, { spec }, { "If-Match": created.data.draft_etag });
    expect(put.data.draft_spec.title).toBe("Changed");
    expect(put.data.draft_etag).not.toBe(created.data.draft_etag);

    const bad = { ...spec, channels: [{ name: "radio", label: "Radio" }] };
    await call("ana", "PUT", `/v1/dashboards/${id}/draft`, { spec: bad }, { "If-Match": put.data.draft_etag });
    const v = await call<{ ok: boolean; issues: { pointer: string }[] }>("ana", "POST", `/v1/dashboards/${id}:validate`);
    expect(v.data.ok).toBe(false);
    expect(v.data.issues[0]?.pointer).toBe("/channels/0/name");
    const again = (await call<Dashboard>("ana", "GET", `/v1/dashboards/${id}`)).data;
    await call("ana", "PUT", `/v1/dashboards/${id}/draft`, { spec }, { "If-Match": again.draft_etag });

    const pub = await call<Dashboard>("rui", "POST", `/v1/dashboards/${id}:publish`);
    expect(pub.status).toBe(200);
    expect(pub.data).toMatchObject({ status: "published", run_as: PERSONAS.rui!.id });
    expect(pub.data.latest_readout).toMatchObject({ kind: "baseline", status: "queued" });
    await wait(60);
    const rl = await call<ReadoutList>("rui", "GET", `/v1/dashboards/${id}/readouts`);
    expect(rl.data.items?.[0]?.status).toBe("ready");
    expect((await call<{ items: unknown[] }>("rui", "GET", `/v1/dashboards/${id}/revisions`)).data.items).toHaveLength(1);
  });

  it("grant to project members only, and manage share links", async () => {
    expect((await call("ana", "PUT", `/v1/dashboards/${FORECAST_DASHBOARD_ID}/grants/usr_nobody`)).status).toBe(409);
    const g = await call<{ user_id: string }>("ana", "PUT", `/v1/dashboards/${FORECAST_DASHBOARD_ID}/grants/${PERSONAS.vic!.id}`);
    expect(g.data.user_id).toBe(PERSONAS.vic!.id);
    expect((await call("ana", "DELETE", `/v1/dashboards/${FORECAST_DASHBOARD_ID}/grants/${PERSONAS.vic!.id}`)).status).toBe(204);

    const short = await call<{ code: string }>("ana", "POST", `/v1/dashboards/${MMM_DASHBOARD_ID}/shares`, { passcode: "1234567" });
    expect(short.data.code).toBe("SCHEMA_INVALID");
    const made = await call<DashboardShareCreated>("ana", "POST", `/v1/dashboards/${MMM_DASHBOARD_ID}/shares`, { label: "Test", expires_in_days: 7 });
    expect(made.status).toBe(201);
    expect(made.data).toMatchObject({ has_passcode: false, allowed_inputs: [], status: "active" });
    expect(made.data.url_fragment).toBe(`/s#${made.data.token}`);
    const list = await call<{ items: Record<string, unknown>[] }>("ana", "GET", `/v1/dashboards/${MMM_DASHBOARD_ID}/shares`);
    expect(list.data.items.some((s) => "token" in s)).toBe(false);
    const open = await fetch(`${env.apiBase}/v1/share-sessions`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token: made.data.token }) });
    expect(open.status).toBe(200);
    const { session } = (await open.json()) as { session: string };
    const revoked = await call<{ status: string }>("ana", "POST", `/v1/dashboards/${MMM_DASHBOARD_ID}/shares/${made.data.id}:revoke`);
    expect(revoked.data.status).toBe("revoked");
    const after = await fetch(`${env.apiBase}/v1/shared/dashboard`, { headers: { "X-Mapi-Share": session } });
    expect(after.status).toBe(410);
  });

  it("review pending readouts and approve a refit request", async () => {
    const pending = await call<ReadoutList>("ana", "GET", `/v1/dashboards/${MMM_DASHBOARD_ID}/readouts?visibility=pending_review`);
    expect(pending.data.items?.map((r) => r.seq)).toEqual([13]);
    const rq = await call<{ items: DashboardRefitRequest[] }>("ana", "GET", `/v1/dashboards/${MMM_DASHBOARD_ID}/refit-requests?status=open`);
    expect(rq.data.items).toHaveLength(1);
    const reqId = rq.data.items[0]!.id;
    const scenario = await call<{ code: string }>("ana", "POST", `/v1/dashboards/${MMM_DASHBOARD_ID}/readouts/14:publish`);
    expect(scenario.status).toBe(409);
    const ok = await call<DashboardRefitRequest>("ana", "POST", `/v1/dashboards/${MMM_DASHBOARD_ID}/refit-requests/${reqId}:approve`, { note: "Thanks" });
    expect(ok.data).toMatchObject({ status: "approved", decided_by: PERSONAS.ana!.id });
    expect(ok.data.workflow_run_id).toBeTruthy();
    const r13 = await call<Readout>("ana", "GET", `/v1/dashboards/${MMM_DASHBOARD_ID}/readouts/13`);
    expect(r13.data).toMatchObject({ visibility: "published", refit_status: "approved" });
    const hidden = await call<ReadoutHeader>("ana", "POST", `/v1/dashboards/${MMM_DASHBOARD_ID}/readouts/13:hide`);
    expect(hidden.data.visibility).toBe("hidden");
    expect((await call("cleo", "GET", `/v1/dashboards/${MMM_DASHBOARD_ID}/readouts/13`)).status).toBe(404);
  });

  it("refresh a dashboard (editor) and see the readout settle", async () => {
    expect((await call("vic", "POST", `/v1/dashboards/${MMM_DASHBOARD_ID}:refresh`)).status).toBe(403);
    const h = await call<ReadoutHeader>("ana", "POST", `/v1/dashboards/${MMM_DASHBOARD_ID}:refresh`, undefined, { "Idempotency-Key": "r-1" });
    expect(h.status).toBe(202);
    const again = await call<ReadoutHeader>("ana", "POST", `/v1/dashboards/${MMM_DASHBOARD_ID}:refresh`, undefined, { "Idempotency-Key": "r-1" });
    expect(again.data.seq).toBe(h.data.seq);
    await wait(60);
    const r = await call<Readout>("ana", "GET", `/v1/dashboards/${MMM_DASHBOARD_ID}/readouts/${h.data.seq}`);
    expect(r.data.status).toBe("ready");
    expect(r.data.snapshot?.tiles?.length).toBeGreaterThan(0);
  });

  it("let viewers use the inputs but refuse out-of-range plans", async () => {
    const ok = await call<{ delta_kpi: number }>("vic", "POST", `/v1/dashboards/${MMM_DASHBOARD_ID}/whatif:evaluate`, { multipliers: { tv: 1.2 } });
    expect(ok.status).toBe(200);
    const out = await call<{ code: string }>("vic", "POST", `/v1/dashboards/${MMM_DASHBOARD_ID}/whatif:evaluate`, { multipliers: { tv: 4 } });
    expect(out.data.code).toBe("SCENARIO_OUT_OF_RANGE");
    const none = await call<{ code: string }>("vic", "GET", `/v1/dashboards/${FORECAST_DASHBOARD_ID}/grid`);
    expect(none).toMatchObject({ status: 409, data: { code: "DASHBOARD_INPUT_DISABLED" } });
  });
});

describe("fixture maths", () => {
  it("places zones as zone_of does", () => {
    expect(zoneOf(0.8, 0.5, 1)).toBe("no_payback");
    expect(zoneOf(1.5, 0.9, 1)).toBe("over");
    expect(zoneOf(1.5, 1.3, 1)).toBe("headroom");
    expect(zoneOf(1.5, 1.1, 1)).toBe("optimal");
    expect(zoneOf(null, 1.1, 1)).toBeNull();
  });

  it("answers the current plan with no change", async () => {
    const d = (await call<Dashboard>("ana", "GET", `/v1/dashboards/${MMM_DASHBOARD_ID}`)).data;
    const grid = (await call<Parameters<typeof whatIfOf>[1]>("ana", "GET", `/v1/dashboards/${MMM_DASHBOARD_ID}/grid`)).data;
    const w = whatIfOf(layout1Spec(d.draft_spec), grid, {});
    expect(Math.abs(w.delta_kpi)).toBeLessThan(1e-6);
    expect(w.lead).toBe("This plan brings the same result as the current plan.");
    expect(w.channels?.map((c) => c.zone)).toEqual(expect.arrayContaining([expect.stringMatching(/headroom|optimal|over|no_payback/)]));
  });
});
