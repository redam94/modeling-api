import { setupServer } from "msw/node";
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";

import { env } from "@/env";
import {
  applyOnboardingPatch,
  checklistDone,
  emptyOnboarding,
  EVENT_STEP,
  missingSteps,
  sampleFraction,
  sampleModelPath,
  type OnboardingState,
  type SampleInfo,
} from "@/features/onboarding/api";
import { ONBOARDING_EVENTS } from "@/lib/onboarding-events";

import { createHandlers, createState } from "./handlers";
import { MOCK_FRESH_USER_KEY, MOCK_ONBOARDING_KEY, MOCK_SAMPLE_FAIL_KEY, MOCK_SAMPLE_MS_KEY, MOCK_SAMPLES_KEY, MOCK_SAMPLES_OFF_KEY } from "./onboarding";
import { MOCK_SESSION_KEY, mockCsrfToken, PERSONAS } from "./tenancy";

let state = createState();
const server = setupServer();
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterAll(() => server.close());
beforeEach(() => {
  localStorage.clear();
  localStorage.setItem(MOCK_SAMPLE_MS_KEY, "200");
  state = createState();
  server.resetHandlers(...createHandlers(state));
});

type Who = keyof typeof PERSONAS;

async function call<T>(who: Who, method: string, path: string, body?: unknown, headers: Record<string, string> = {}): Promise<{ status: number; data: T; headers: Headers }> {
  localStorage.setItem(MOCK_SESSION_KEY, who);
  const res = await fetch(`${env.apiBase}${path}`, {
    method,
    headers: { "X-CSRF-Token": mockCsrfToken(PERSONAS[who]!), ...(body !== undefined ? { "Content-Type": "application/json" } : {}), ...headers },
    body: body === undefined ? null : JSON.stringify(body),
  });
  const text = await res.text();
  return { status: res.status, data: (text ? JSON.parse(text) : null) as T, headers: res.headers };
}

const wait = (ms: number) => new Promise((r) => setTimeout(r, ms));
const ONB = "/v1/tenants/acme/me/onboarding";

describe("onboarding progress (mock)", () => {
  it("starts welcomed with tips off, so no existing spec sees a dialog (D24)", async () => {
    const r = await call<OnboardingState>("ana", "GET", ONB);
    expect(r.status).toBe(200);
    expect(r.data.welcomed_at).toBeTruthy();
    expect(r.data.tips_off).toBe(true);
    expect(r.headers.get("ETag")).toBe(`"${r.data.version}"`);
  });

  it("starts empty for a fresh user (mapi.mock.freshUser=1)", async () => {
    localStorage.setItem(MOCK_FRESH_USER_KEY, "1");
    const r = await call<OnboardingState>("ana", "GET", ONB);
    expect(r.data).toMatchObject({ version: 0, welcomed_at: null, tips_off: false });
  });

  it("merges, never un-completes, and survives a reload", async () => {
    localStorage.setItem(MOCK_FRESH_USER_KEY, "1");
    const a = await call<OnboardingState>("ana", "PATCH", ONB, { complete: ["open_sample"], welcomed: true });
    expect(a.status).toBe(200);
    expect(a.data.version).toBe(1);
    expect(Object.keys(a.data.checklist ?? {})).toEqual(["open_sample"]);
    const b = await call<OnboardingState>("ana", "PATCH", ONB, { tours: { welcome: { status: "completed", step: 4, updated_at: "2026-10-20T00:00:00Z" } } });
    expect(Object.keys(b.data.checklist ?? {})).toEqual(["took_tour", "open_sample"]);
    // the same patch again changes nothing
    const c = await call<OnboardingState>("ana", "PATCH", ONB, { complete: ["open_sample"] });
    expect(c.data.version).toBe(b.data.version);
    expect(localStorage.getItem(MOCK_ONBOARDING_KEY)).toContain("open_sample");
    // a new page (fresh mock state) reads the stored progress
    server.resetHandlers(...createHandlers(createState()));
    const d = await call<OnboardingState>("ana", "GET", ONB);
    expect(Object.keys(d.data.checklist ?? {})).toEqual(["took_tour", "open_sample"]);
    // progress is per person
    const vic = await call<OnboardingState>("vic", "GET", ONB);
    expect(vic.data.checklist).toEqual({});
  });

  it("refuses a stale If-Match, unknown names and a large body", async () => {
    const r = await call<OnboardingState>("ana", "GET", ONB);
    expect((await call("ana", "PATCH", ONB, { complete: ["fit_model"] }, { "If-Match": '"99"' })).status).toBe(412);
    expect((await call("ana", "PATCH", ONB, { complete: ["fit_model"] }, { "If-Match": `"${r.data.version}"` })).status).toBe(200);
    expect((await call("ana", "PATCH", ONB, { complete: ["eat_cake"] })).status).toBe(422);
    expect((await call("ana", "PATCH", ONB, { tours: { nope: { status: "completed", step: 1, updated_at: "x" } } })).status).toBe(422);
    expect((await call("ana", "PATCH", ONB, { hide_samples: Array.from({ length: 300 }, (_, i) => `sample-${i}`) })).status).toBe(413);
    expect((await call("ana", "PATCH", ONB, { hide_samples: Array.from({ length: 9 }, (_, i) => `s${i}`) })).status).toBe(422);
  });

  it("resets the checklist and tours but keeps the welcome", async () => {
    await call("ana", "PATCH", ONB, { complete: ["fit_model"], tips_off: true });
    const r = await call<OnboardingState>("ana", "POST", `${ONB}:reset`);
    expect(r.status).toBe(200);
    expect(r.data.checklist).toEqual({});
    expect(r.data.tips_off).toBe(false);
    expect(r.data.welcomed_at).toBeTruthy();
  });

  it("refuses people outside the tenant and client users", async () => {
    expect((await call("rui", "GET", "/v1/tenants/globex/me/onboarding")).status).toBe(403);
    expect((await call("cleo", "GET", ONB)).status).toBe(403);
  });
});

describe("sample projects (mock)", () => {
  const SAMPLES = "/v1/tenants/acme/samples";

  it("installs, moves through the states and lands on a model in the sample project", async () => {
    const before = await call<SampleInfo[]>("ana", "GET", SAMPLES);
    expect(before.data[0]).toMatchObject({ id: "geo_mmm", state: null, can_install: true });
    const inst = await call<SampleInfo>("ana", "POST", `${SAMPLES}/geo_mmm:install`);
    expect(inst.status).toBe(202);
    expect(inst.data.state).toBe("provisioning");
    expect(state.tenancy.projects.has("acme/sample")).toBe(true);
    const seen = new Set<string>();
    for (let i = 0; i < 40; i++) {
      const r = await call<SampleInfo[]>("ana", "GET", SAMPLES);
      seen.add(String(r.data[0]?.state));
      if (r.data[0]?.state === "ready") break;
      await wait(15);
    }
    expect(seen.has("ready")).toBe(true);
    const ready = (await call<SampleInfo[]>("ana", "GET", SAMPLES)).data[0]!;
    expect(sampleModelPath(ready)).toMatch(/^\/models\/mdl_.+\/versions\/1$/);
    expect(sampleFraction(ready)).toBe(1);
    // the seed library items are in the sample project
    const lib = await call<{ items: { created_by: string }[] }>("ana", "GET", "/v1/library", undefined, { "X-Mapi-Project": "acme/sample" });
    expect(lib.status).toBe(200);
    expect(lib.data.items.filter((i) => i.created_by === "system")).toHaveLength(3);
    // a second install joins (200), no second fit
    expect((await call("ana", "POST", `${SAMPLES}/geo_mmm:install`)).status).toBe(200);
    expect(localStorage.getItem(MOCK_SAMPLES_KEY)).toContain("acme");
  });

  it("needs an editor to install; a viewer may join an installed sample", async () => {
    expect((await call("vic", "POST", `${SAMPLES}/geo_mmm:install`)).status).toBe(403);
    expect((await call<SampleInfo[]>("vic", "GET", SAMPLES)).data[0]?.can_install).toBe(false);
    await call("ana", "POST", `${SAMPLES}/geo_mmm:install`);
    expect((await call("vic", "POST", `${SAMPLES}/geo_mmm:install`)).status).toBe(200);
  });

  it("fails plainly and retries (mapi.mock.sampleFail=1)", async () => {
    localStorage.setItem(MOCK_SAMPLE_FAIL_KEY, "1");
    await call("ana", "POST", `${SAMPLES}/geo_mmm:install`);
    await wait(150);
    const failed = (await call<SampleInfo[]>("ana", "GET", SAMPLES)).data[0]!;
    expect(failed.state).toBe("failed");
    expect(failed.problem?.code).toBe("QUOTA_EXCEEDED");
    expect(failed.can_retry).toBe(true);
    expect((await call("rui", "POST", `${SAMPLES}/geo_mmm:retry`)).status).toBe(403);
    const retry = await call<SampleInfo>("ana", "POST", `${SAMPLES}/geo_mmm:retry`);
    expect(retry.status).toBe(202);
    expect(retry.data.attempts).toBe(2);
  });

  it("retires (tenant admins only) and can be turned off", async () => {
    await call("ana", "POST", `${SAMPLES}/geo_mmm:install`);
    expect((await call("rui", "POST", `${SAMPLES}/geo_mmm:retire`)).status).toBe(403);
    expect((await call<SampleInfo>("ana", "POST", `${SAMPLES}/geo_mmm:retire`)).data.state).toBe("retired");
    localStorage.setItem(MOCK_SAMPLES_OFF_KEY, "1");
    expect((await call<SampleInfo[]>("ana", "GET", SAMPLES)).data).toEqual([]);
    expect((await call("ana", "POST", `${SAMPLES}/geo_mmm:install`)).status).toBe(404);
  });
});

describe("onboarding helpers", () => {
  it("mirrors the server's merge", () => {
    const s0 = emptyOnboarding();
    const s1 = applyOnboardingPatch(s0, { complete: ["fit_model", "fit_model"] }, "2026-10-20T00:00:00Z");
    expect(s1.version).toBe(1);
    expect(applyOnboardingPatch(s1, { complete: ["fit_model"] })).toBe(s1);
    const s2 = applyOnboardingPatch(s1, { hide_samples: ["geo_mmm"], unhide_samples: ["geo_mmm"] });
    expect(s2).toBe(s1);
    const s3 = applyOnboardingPatch(s1, { checklist_dismissed: true });
    expect(checklistDone(s3)).toBe(true);
    expect(missingSteps(s1, ["fit_model", "own_data", "own_data"])).toEqual(["own_data"]);
  });

  it("maps every onboarding event", () => {
    expect(Object.keys(EVENT_STEP).sort()).toEqual([...ONBOARDING_EVENTS].sort());
    expect(EVENT_STEP.library_item_saved).toBe("make_your_own");
  });
});
