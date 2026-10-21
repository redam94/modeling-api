import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { createHandlers } from "@/mocks/handlers";
import { FORECAST_DASHBOARD_ID, MMM_DASHBOARD_ID, MOCK_SHARE_PASSCODE, MOCK_SHARE_TOKENS } from "@/mocks/dashboards";
import { MOCK_SESSION_KEY, mockCsrfToken, PERSONAS } from "@/mocks/tenancy";
import { useAuth } from "@/stores/auth";

import { effectiveInputs, memberClient, openShareSession, problemOf, refreshShareSession, shareClient, ShareApiError, toClientDashboard, visibleTabs } from "./client";
import { isFullReadout, READOUT_POLL_MAX_MS, READOUT_POLL_MS, readoutPollMs, SHARE_SESSION_RE, type Dashboard, type DashboardSpec, type Readout, type ShareSessionHandle } from "./types";

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
  localStorage.setItem("mapi.mock.readoutMs", "40");
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

/** A share session handle over the mock (refresh swaps in a new session). */
async function handleFor(token: string, passcode: string | null = null): Promise<ShareSessionHandle & { session: string }> {
  const first = await openShareSession(token, passcode);
  const h = {
    session: first.session,
    current: () => h.session,
    expired: () => false,
    refresh: async () => {
      h.session = (await refreshShareSession(h.session)).session;
    },
  };
  return h;
}

async function until<T>(fn: () => Promise<T>, ok: (v: T) => boolean, tries = 50): Promise<T> {
  for (let i = 0; i < tries; i++) {
    const v = await fn();
    if (ok(v)) return v;
    await new Promise((r) => setTimeout(r, 30));
  }
  throw new Error("timed out");
}

describe("shareClient", () => {
  it("never sends cookies, the CSRF header or the project header", async () => {
    // A signed-in analyst in the same browser: the share client must still send none of it.
    localStorage.setItem(MOCK_SESSION_KEY, "ana");
    useAuth.setState({ mode: "session", csrf: mockCsrfToken(PERSONAS.ana!) });
    const spy = vi.spyOn(globalThis, "fetch");
    const h = await handleFor(MOCK_SHARE_TOKENS.inputs, MOCK_SHARE_PASSCODE);
    const c = shareClient(h, MMM_DASHBOARD_ID);
    await c.dashboard();
    await c.readouts({ limit: 5 });
    await c.evaluate({ tv: 1.1 });
    await c.saveScenario({ multipliers: { search: 1.2 } }, "k-1");
    expect(spy).toHaveBeenCalled();
    for (const [input, init] of spy.mock.calls) {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      expect(url).toMatch(/\/v1\/(shared|share-sessions)/);
      expect(init?.credentials).toBe("omit");
      expect(init?.referrerPolicy).toBe("no-referrer");
      const headers = new Headers(init?.headers);
      expect(headers.has("X-CSRF-Token")).toBe(false);
      expect(headers.has("X-Mapi-Project")).toBe(false);
      expect(headers.has("Cookie")).toBe(false);
    }
    const shared = seen.filter((r) => new URL(r.url).pathname.startsWith("/v1/shared/"));
    expect(shared.length).toBe(4);
    for (const r of shared) {
      expect(r.headers.get("X-Mapi-Share")).toBe(h.session);
      expect(r.credentials).toBe("omit");
    }
    expect(SHARE_SESSION_RE.test(h.session)).toBe(true);
    useAuth.setState({ mode: "unknown", csrf: null });
  });

  it("asks for the passcode, refuses a wrong one and opens with the right one", async () => {
    const need = await openShareSession(MOCK_SHARE_TOKENS.inputs).catch((e: unknown) => e);
    expect(need).toBeInstanceOf(ShareApiError);
    expect(problemOf(need)).toMatchObject({ status: 401, code: "SHARE_PASSCODE_REQUIRED" });
    const wrong = await openShareSession(MOCK_SHARE_TOKENS.inputs, "not-the-code").catch((e: unknown) => e);
    expect(problemOf(wrong)?.code).toBe("SHARE_PASSCODE_INVALID");
    const ok = await openShareSession(MOCK_SHARE_TOKENS.inputs, MOCK_SHARE_PASSCODE);
    expect(ok.dashboard.inputs).toMatchObject({ whatif: true, actuals: true, refit_requests: true });
    expect(ok.dashboard.ends_at).toBeTruthy();
  });

  it("reports an ended link as 410 LINK_EXPIRED", async () => {
    const gone = await openShareSession(MOCK_SHARE_TOKENS.revoked).catch((e: unknown) => e);
    expect(problemOf(gone)).toMatchObject({ status: 410, code: "LINK_EXPIRED" });
  });

  it("shows a read-only link without what-ifs or uploads", async () => {
    const h = await handleFor(MOCK_SHARE_TOKENS.readOnly);
    const c = shareClient(h);
    const d = await c.dashboard();
    expect(d.inputs).toMatchObject({ whatif: false, actuals: false });
    expect(d.tabs).toEqual(["this_period", "trends", "history"]);
    expect(await c.grid()).toBeNull();
    const refused = await c.evaluate({ tv: 1.2 }).catch((e: unknown) => e);
    expect(problemOf(refused)?.code).toBe("DASHBOARD_INPUT_DISABLED");
    // Other people's private and pending readouts stay hidden.
    const list = await c.readouts({ limit: 50 });
    expect(list.every((r) => r.visibility === "published" && r.status !== "failed")).toBe(true);
  });

  it("refreshes an ended session once and retries", async () => {
    const h = await handleFor(MOCK_SHARE_TOKENS.readOnly);
    const stale = h.session;
    // The session ends between two requests: the mock forgets it.
    h.session = `${stale.slice(0, -4)}AAAA`;
    let refreshed = 0;
    const handle: ShareSessionHandle = {
      current: () => h.session,
      expired: () => false,
      refresh: async () => {
        refreshed += 1;
        h.session = (await openShareSession(MOCK_SHARE_TOKENS.readOnly)).session;
      },
    };
    const d = await shareClient(handle).dashboard();
    expect(d.id).toBe(MMM_DASHBOARD_ID);
    expect(refreshed).toBe(1);
  });

  it("runs an exact scenario (202, then the readout) and sends actuals with a refit request", async () => {
    const c = shareClient(await handleFor(MOCK_SHARE_TOKENS.inputs, MOCK_SHARE_PASSCODE));
    const queued = await c.saveScenario({ multipliers: { tv: 0.8, search: 1.4 }, exact: true, label: "Exact plan" }, "exact-1");
    expect(isFullReadout(queued)).toBe(false);
    expect(queued.status).toBe("queued");
    const again = await c.saveScenario({ multipliers: { tv: 0.8, search: 1.4 }, exact: true }, "exact-1");
    expect(again.seq).toBe(queued.seq);
    const ready = await until(() => c.readout(queued.seq), (r) => r.status === "ready");
    expect(ready.snapshot?.scenario).toEqual({ tv: 0.8, search: 1.4 });
    expect(ready.visibility).toBe("private");

    const t = await c.actualsTemplate();
    expect(t.mode).toBe("compared");
    const header = await c.submitActuals(
      {
        inputs: { coords: { date: { dims: ["date"], data: ["2026-10-04", "2026-10-11", "2026-10-18"] } }, data_vars: { revenue: { dims: ["date"], data: [401_000, 415_500, 398_200] } } },
        request_refit: true,
      },
      "act-1",
    );
    expect(header).toMatchObject({ kind: "actuals", visibility: "pending_review", refit_status: "open" });
    const done = await until(() => c.readout(header.seq), (r) => r.status === "ready");
    expect(done.snapshot?.accuracy?.n).toBe(3);
  });

  it("conditions a forecast on actuals and publishes it at once", async () => {
    const c = shareClient(await handleFor(MOCK_SHARE_TOKENS.forecast), FORECAST_DASHBOARD_ID);
    const d = await c.dashboard();
    expect(d.inputs).toMatchObject({ whatif: false, actuals: true, actuals_mode: "conditioned" });
    const t = await c.actualsTemplate();
    expect(t.kpi_column).toBe("sales");
    const h = await c.submitActuals({ inputs: { coords: { month: { dims: ["month"], data: ["2026-09-01"] } }, data_vars: { sales: { dims: ["month"], data: [190_100] } } } }, "f-1");
    expect(h.visibility).toBe("published");
    const r = await until(() => c.readout(h.seq), (x) => x.status === "ready");
    expect(r.snapshot?.conditioning?.conditioned).toBe(true);
    const bad = await c.submitActuals({ inputs: { data_vars: { price: { dims: ["month"], data: [1] } } } }, "f-2").catch((e: unknown) => e);
    expect(problemOf(bad)).toMatchObject({ status: 422, code: "ACTUALS_INVALID" });
  });
});

describe("memberClient", () => {
  beforeEach(() => {
    useAuth.setState({ mode: "session", csrf: null });
  });
  afterEach(() => useAuth.setState({ mode: "unknown", csrf: null }));

  it("gives a client user the published dashboard and their own readouts", async () => {
    localStorage.setItem(MOCK_SESSION_KEY, "cleo");
    useAuth.setState({ csrf: mockCsrfToken(PERSONAS.cleo!) });
    const c = memberClient("acme/growth", MMM_DASHBOARD_ID, { kind: "client" });
    const d = await c.dashboard();
    expect(d.latest_readout?.status).toBe("ready");
    const list = await c.readouts({ limit: 50 });
    // Cleo sees her pending upload and her private scenario, not the failed refresh.
    expect(list.map((r) => r.visibility)).toContain("pending_review");
    expect(list.map((r) => r.visibility)).toContain("private");
    expect(list.some((r) => r.status === "failed")).toBe(false);
    const grid = await c.grid();
    expect(grid?.channels).toEqual(["tv", "search", "social", "display"]);
    const w = await c.evaluate({ search: 1.5 });
    expect(w.delta_kpi).toBeGreaterThan(0);
    const saved = await c.saveScenario({ multipliers: { search: 1.5 } }, "m-1");
    expect(isFullReadout(saved) && saved.snapshot?.scenario).toEqual({ search: 1.5 });
    for (const r of seen.filter((x) => new URL(x.url).pathname.startsWith("/v1/dashboards"))) {
      expect(r.headers.get("X-Mapi-Project")).toBe("acme/growth");
    }
  });

  it("shows an analyst what a client would see (the preview)", async () => {
    localStorage.setItem(MOCK_SESSION_KEY, "ana");
    useAuth.setState({ csrf: mockCsrfToken(PERSONAS.ana!) });
    const d = await memberClient("acme/growth", MMM_DASHBOARD_ID).dashboard();
    expect(d.title).toBe("Weekly media readout");
    expect(d.latest_readout?.visibility).toBe("published");
    expect(d.latest_readout?.status).toBe("ready");
    expect(d.tabs?.[0]).toBe("this_period");
  });

  it("answers null for a dashboard without a response grid", async () => {
    localStorage.setItem(MOCK_SESSION_KEY, "ana");
    expect(await memberClient("acme/growth", FORECAST_DASHBOARD_ID).grid()).toBeNull();
  });
});

describe("pure helpers", () => {
  const spec = {
    title: "T",
    source: { kind: "endpoint", alias: "a" },
    kpi: { variable: "y", label: "Y", unit: "count" },
    channels: [{ name: "tv", label: "TV" }],
    tabs: ["this_period", "scenarios", "update", "history"],
    whatif: { enabled: true, exact: true },
    actuals: { enabled: true, publish: "review", allow_refit_requests: true },
  } as DashboardSpec;

  it("narrows inputs by a share link's allowed inputs", () => {
    expect(effectiveInputs(spec)).toEqual({ whatif: true, exact: true, actuals: true, actuals_mode: "compared", refit_requests: true });
    const ro = effectiveInputs(spec, null, []);
    expect(ro).toEqual({ whatif: false, exact: false, actuals: false, actuals_mode: null, refit_requests: false });
    expect(visibleTabs(spec.tabs!, ro)).toEqual(["this_period", "history"]);
  });

  it("builds the client view of an analyst's dashboard", () => {
    const d = { id: "dsh_1", name: "n", status: "draft", draft_spec: spec, draft_etag: "e", created_by: "u", created_at: "t", updated_at: "t", live_inputs: "paused" } as Dashboard;
    const latest = { seq: 1 } as Readout;
    const v = toClientDashboard(d, latest);
    expect(v).toMatchObject({ id: "dsh_1", title: "T", live_inputs: "paused", latest_readout: latest, tabs: ["this_period", "scenarios", "update", "history"] });
  });

  it("reads the problem of any dashboard error", () => {
    expect(problemOf(new ShareApiError(409, { type: "x", title: "t", status: 409, code: "DASHBOARD_INPUT_DISABLED" }))).toMatchObject({ status: 409, code: "DASHBOARD_INPUT_DISABLED" });
    expect(problemOf(new Error("x"))).toBeNull();
    expect(problemOf("nope")).toBeNull();
  });
});

describe("readoutPollMs", () => {
  it("backs off from 2 s to 15 s so a long run stays well inside the share link's address budget", () => {
    expect(readoutPollMs(0)).toBe(READOUT_POLL_MS);
    expect(readoutPollMs(1)).toBe(3_000);
    expect(readoutPollMs(50)).toBe(READOUT_POLL_MAX_MS);
    // the first minute of a pending readout: far below the 120 requests per address
    let t = 0;
    let polls = 0;
    while (t < 60_000) t += readoutPollMs(polls++);
    expect(polls).toBeLessThanOrEqual(10);
  });
});
