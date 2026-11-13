/**
 * MSW emulation of onboarding progress and sample projects (v0.18 D19, D20, I-5):
 * `GET/PATCH /v1/tenants/{t}/me/onboarding`, `POST …:reset`, `GET /v1/tenants/{t}/samples`
 * and `POST …/samples/{id}:install|:retry|:retire`.
 *
 * **Welcomed by default (D24, critic).** The Playwright smoke suite has no global setup, so
 * a persona's progress starts as a user who already closed the welcome dialog and turned
 * tips off: no dialog and no tour ever appear in an existing spec. A spec (or a person
 * trying the mock) opts into a first visit with
 * `localStorage.setItem("mapi.mock.freshUser", "1")` before the page loads.
 *
 * Progress and installed samples survive a reload (localStorage `mapi.mock.onboarding`,
 * `mapi.mock.samples`), like the server's rows, so "ticks persist across reload" can be
 * tested. The sample moves `provisioning → ingesting → fitting → ready` in about six
 * seconds (`mapi.mock.sampleMs`), then points at the study fit of the fixtures (model
 * `eu-geo-mmm` v1) in the project `sample`, which joins the project switcher, and seeds
 * the three library items into it. Switches: `mapi.mock.sampleFail=1` makes the first fit
 * fail with a plain quota problem (retry works), `mapi.mock.samplesOff=1` turns samples off.
 */
import { delay, http, HttpResponse, type HttpHandler } from "msw";

import type { Problem, ProjectRole } from "@/api/types";
import { env } from "@/env";
import {
  applyOnboardingPatch,
  CHECKLIST_STEPS,
  emptyOnboarding,
  TOUR_IDS,
  type OnboardingPatch,
  type OnboardingState,
  type SampleInfo,
  type SampleState,
} from "@/features/onboarding/api";

import * as fx from "./fixtures";
import { mockPersonaId, PERSONAS } from "./tenancy";

const API = env.apiBase;
const API_RE = API.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const SEG = "([a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?)";
const RESET_RE = new RegExp(`^${API_RE}/v1/tenants/${SEG}/me/onboarding:reset$`);
const SAMPLE_ACTION_RE = (action: string) => new RegExp(`^${API_RE}/v1/tenants/${SEG}/samples/${SEG}:${action}$`);

/** `[tenant, sample]` of a colon route (MSW path patterns cannot hold a literal `:`). */
function groups(re: RegExp, url: string): [string, string] {
  const m = re.exec(url.split("?")[0] ?? "");
  return [decodeURIComponent(m?.[1] ?? ""), decodeURIComponent(m?.[2] ?? "")];
}

export const MOCK_FRESH_USER_KEY = "mapi.mock.freshUser";
export const MOCK_ONBOARDING_KEY = "mapi.mock.onboarding";
export const MOCK_SAMPLES_KEY = "mapi.mock.samples";
export const MOCK_SAMPLE_MS_KEY = "mapi.mock.sampleMs";
export const MOCK_SAMPLE_FAIL_KEY = "mapi.mock.sampleFail";
export const MOCK_SAMPLES_OFF_KEY = "mapi.mock.samplesOff";

/** The project a sample is installed in (`mapi_spec.samples.SAMPLE_PROJECT_ID`). */
export const SAMPLE_PROJECT = "sample";
export const SAMPLE_ID = "geo_mmm";
const MAX_ATTEMPTS = 3;
const PATCH_MAX_BYTES = 2048;
/** When a welcomed-by-default persona closed the welcome dialog. */
const WELCOMED_AT = "2026-10-01T09:00:00Z";

const ROLES: ProjectRole[] = ["client", "viewer", "editor", "runner", "admin"];
const rank = (r: ProjectRole | null | undefined) => (r ? ROLES.indexOf(r) : -1);

type Persona = NonNullable<(typeof PERSONAS)[string]>;

function problem(status: number, code: Problem["code"], title: string, detail?: string, errors: Problem["errors"] = []) {
  return HttpResponse.json({ type: `urn:mapi:error:${code}`, title, status, code, detail: detail ?? null, errors } satisfies Problem, {
    status,
    headers: { "content-type": "application/problem+json" },
  });
}

function read(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    // storage unavailable: progress lives for this page only
  }
}

function readJson<T>(key: string): T | null {
  const raw = read(key);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as T;
  } catch {
    return null;
  }
}

// ----------------------------------------------------------------------------- state

/** One installed sample (per tenant), as the server's `tenant_samples` row. */
export interface SampleRecord {
  installedAt: number;
  installer: string;
  attempts: number;
  retired: boolean;
  /** The library seed was added (once, on the first `ready`). */
  seeded: boolean;
}

export interface OnboardingMockState {
  /** `<principal>/<tenant>` → progress. */
  progress: Map<string, OnboardingState>;
  /** tenant → the installed sample. */
  samples: Map<string, SampleRecord>;
}

export function createOnboardingState(): OnboardingMockState {
  const progress = new Map(Object.entries(readJson<Record<string, OnboardingState>>(MOCK_ONBOARDING_KEY) ?? {}));
  const samples = new Map(Object.entries(readJson<Record<string, SampleRecord>>(MOCK_SAMPLES_KEY) ?? {}));
  return { progress, samples };
}

function save(state: OnboardingMockState): void {
  write(MOCK_ONBOARDING_KEY, JSON.stringify(Object.fromEntries(state.progress)));
  write(MOCK_SAMPLES_KEY, JSON.stringify(Object.fromEntries(state.samples)));
}

/** A first visit (`mapi.mock.freshUser=1`) or a user who already closed the welcome (default). */
export function defaultOnboarding(): OnboardingState {
  if (read(MOCK_FRESH_USER_KEY) === "1") return emptyOnboarding();
  return { ...emptyOnboarding(), version: 1, tips_off: true, welcomed_at: WELCOMED_AT };
}

function progressOf(state: OnboardingMockState, p: Persona, tenant: string): OnboardingState {
  return state.progress.get(`${p.id}/${tenant}`) ?? defaultOnboarding();
}

function sampleMs(): number {
  const v = Number(read(MOCK_SAMPLE_MS_KEY));
  return Number.isFinite(v) && v > 0 ? v : 6000;
}

/** The persona's highest role anywhere in `tenant` (tenant-wide or in one project). */
function roleInTenant(p: Persona, tenant: string): ProjectRole | null {
  let best: ProjectRole | null = p.tenants[tenant] ?? null;
  for (const [key, role] of Object.entries(p.projects)) {
    if (key.startsWith(`${tenant}/`) && rank(role) > rank(best)) best = role;
  }
  return best;
}

function isTenantAdmin(p: Persona, tenant: string): boolean {
  return p.tenants[tenant] === "admin";
}

const SAMPLE_PROBLEM: Problem = {
  type: "urn:mapi:error:QUOTA_EXCEEDED",
  title: "Quota exceeded",
  status: 429,
  code: "QUOTA_EXCEEDED",
  detail: "The sample fit did not start because the tenant used all of its runs for today. Try again after the daily reset.",
  errors: [],
};

/** Where an installed sample is now (time moves on its own). */
export function sampleStage(rec: SampleRecord, now: number = Date.now()): { state: SampleState; progress: number | null; problem: Problem | null } {
  if (rec.retired) return { state: "retired", progress: null, problem: null };
  const total = sampleMs();
  const t = (now - rec.installedAt) / total;
  const failing = read(MOCK_SAMPLE_FAIL_KEY) === "1" && rec.attempts === 1;
  if (t < 0.12) return { state: "provisioning", progress: null, problem: null };
  if (t < 0.28) return { state: "ingesting", progress: null, problem: null };
  if (failing && t >= 0.45) return { state: "failed", progress: null, problem: SAMPLE_PROBLEM };
  if (t < 1) return { state: "fitting", progress: Math.min(0.99, (t - 0.28) / 0.72), problem: null };
  return { state: "ready", progress: 1, problem: null };
}

const SAMPLE_DEF = {
  id: SAMPLE_ID,
  title: "Marketing mix in four regions",
  summary:
    "Two years of weekly revenue with TV, search and social spend in four UK regions. Fit a real model in about a minute, then move budget between channels and see what changes.",
  minutes: 1,
};

/** The ids of the sample's Guided session (seeded in the Guided mock), or the fixtures' study fit. */
export interface SampleTarget {
  specId: string;
  runId: string;
  modelId: string;
  modelVersion: number;
}

function sampleInfo(state: OnboardingMockState, p: Persona, tenant: string, targetOf: () => SampleTarget | null = () => null): SampleInfo {
  const rec = state.samples.get(tenant);
  const role = roleInTenant(p, tenant);
  const hidden = progressOf(state, p, tenant).hidden_samples?.includes(SAMPLE_ID) ?? false;
  if (!rec) {
    return { ...SAMPLE_DEF, state: null, attempts: 0, can_install: rank(role) >= rank("editor"), can_retry: false, can_retire: false, hidden };
  }
  const stage = sampleStage(rec);
  const ready = stage.state === "ready";
  const fitting = stage.state === "fitting" || ready;
  const target = fitting ? targetOf() : null;
  return {
    ...SAMPLE_DEF,
    state: stage.state,
    project_id: SAMPLE_PROJECT,
    spec_id: fitting ? (target?.specId ?? fx.SPEC_ID) : null,
    run_id: fitting ? (target?.runId ?? fx.RUN_DONE) : null,
    model_id: ready ? (target?.modelId ?? fx.MODEL_ID) : null,
    model_version: ready ? (target?.modelVersion ?? 1) : null,
    progress: stage.progress,
    problem: stage.problem,
    attempts: rec.attempts,
    // joining an installed sample needs only the viewer role
    can_install: stage.state !== "retired" && rank(role) >= rank("viewer"),
    can_retry: stage.state === "failed" && rec.attempts < MAX_ATTEMPTS && (rec.installer === p.id || isTenantAdmin(p, tenant)),
    can_retire: stage.state !== "retired" && isTenantAdmin(p, tenant),
    hidden,
  };
}

// ----------------------------------------------------------------------------- handlers

export interface OnboardingDeps {
  /** A sample project now exists in `tenant` (add it to the tenancy mock's projects). */
  addProject: (tenant: string, project: string, name: string) => void;
  /** The sample became ready the first time: seed the library items into its project. */
  seedLibrary: (tenant: string, project: string) => void;
  /** The sample's Guided session in `tenant` (made on first use); `null` keeps the fixtures' ids. */
  session?: (tenant: string) => SampleTarget | null;
}

function persona(): Persona | null {
  const id = mockPersonaId();
  return id ? (PERSONAS[id] ?? null) : null;
}

/** The persona, if they may use the tenant routes of `tenant` (a member or an operator). */
function member(tenant: string): Persona | Response {
  const p = persona();
  if (!p) return problem(401, "UNAUTHENTICATED", "Authentication required", "sign in to continue");
  if (!p.operator && rank(roleInTenant(p, tenant)) < rank("viewer")) {
    return problem(403, "FORBIDDEN", "Not allowed", "You need at least the viewer role in this tenant.");
  }
  return p;
}

const etag = (s: OnboardingState) => `"${s.version ?? 0}"`;

function withEtag(s: OnboardingState, status = 200) {
  return HttpResponse.json(s, { status, headers: { ETag: etag(s) } });
}

function ifMatches(header: string | null, s: OnboardingState): boolean {
  if (!header) return true;
  return header.split(",").some((raw) => {
    const t = raw.trim().replace(/^W\//, "");
    return t === "*" || t === etag(s);
  });
}

/** The PATCH body checks of `OnboardingPatch` (closed names, caps). */
function patchIssues(body: unknown): Problem["errors"] {
  const errors: NonNullable<Problem["errors"]> = [];
  const bad = (pointer: string, message: string) => errors.push({ code: "SCHEMA_INVALID", message, pointer, severity: "error" });
  if (typeof body !== "object" || body === null || Array.isArray(body)) {
    bad("", "The body must be a JSON object.");
    return errors;
  }
  const b = body as Record<string, unknown>;
  const allowed = new Set(["complete", "tours", "checklist_dismissed", "tips_off", "hide_samples", "unhide_samples", "welcomed"]);
  for (const k of Object.keys(b)) if (!allowed.has(k)) bad(`/${k}`, `Unknown field "${k}".`);
  if (b.complete !== undefined) {
    if (!Array.isArray(b.complete)) bad("/complete", "Must be a list.");
    else b.complete.forEach((s, i) => !(CHECKLIST_STEPS as readonly unknown[]).includes(s) && bad(`/complete/${i}`, "Unknown checklist step."));
  }
  if (b.tours != null) {
    if (typeof b.tours !== "object" || Array.isArray(b.tours)) bad("/tours", "Must be an object.");
    else
      for (const [id, t] of Object.entries(b.tours as Record<string, unknown>)) {
        if (!(TOUR_IDS as readonly string[]).includes(id)) bad(`/tours/${id}`, "Unknown tour.");
        const tp = t as { status?: unknown; step?: unknown } | null;
        if (!tp || !["in_progress", "completed", "dismissed"].includes(String(tp.status))) bad(`/tours/${id}/status`, "Unknown tour status.");
        if (tp && tp.step !== undefined && !(Number.isInteger(tp.step) && Number(tp.step) >= 0 && Number(tp.step) <= 50)) bad(`/tours/${id}/step`, "Must be a whole number from 0 to 50.");
      }
  }
  for (const k of ["checklist_dismissed", "tips_off", "welcomed"]) {
    if (b[k] != null && typeof b[k] !== "boolean") bad(`/${k}`, "Must be true or false.");
  }
  for (const k of ["hide_samples", "unhide_samples"]) {
    if (b[k] !== undefined && !(Array.isArray(b[k]) && (b[k] as unknown[]).every((s) => typeof s === "string"))) bad(`/${k}`, "Must be a list of sample ids.");
  }
  return errors;
}

export function createOnboardingHandlers(state: OnboardingMockState, deps: OnboardingDeps): HttpHandler[] {
  const base = `${API}/v1/tenants/:tenant`;
  const samplesOff = () => read(MOCK_SAMPLES_OFF_KEY) === "1";

  /** Seed the library the first time a sample is seen ready. */
  const advance = (tenant: string) => {
    const rec = state.samples.get(tenant);
    if (!rec || rec.seeded || sampleStage(rec).state !== "ready") return;
    rec.seeded = true;
    deps.seedLibrary(tenant, SAMPLE_PROJECT);
    save(state);
  };

  return [
    // ------------------------------------------------------------------ progress
    http.get(`${base}/me/onboarding`, ({ params }) => {
      const tenant = String(params.tenant);
      const p = member(tenant);
      if (p instanceof Response) return p;
      return withEtag(progressOf(state, p, tenant));
    }),
    http.patch(`${base}/me/onboarding`, async ({ params, request }) => {
      const tenant = String(params.tenant);
      const p = member(tenant);
      if (p instanceof Response) return p;
      const text = await request.text();
      if (new TextEncoder().encode(text).length > PATCH_MAX_BYTES) {
        return problem(413, "PAYLOAD_TOO_LARGE", "Payload too large", `the onboarding update is larger than ${PATCH_MAX_BYTES} bytes`);
      }
      let body: unknown;
      try {
        body = JSON.parse(text || "{}");
      } catch {
        return problem(422, "SCHEMA_INVALID", "Invalid request", "The body is not valid JSON.");
      }
      const errors = patchIssues(body);
      if (errors?.length) return problem(422, "SCHEMA_INVALID", "Invalid request", errors[0]?.message, errors);
      const current = progressOf(state, p, tenant);
      if (!ifMatches(request.headers.get("If-Match"), current)) {
        return problem(412, "PRECONDITION_FAILED", "Precondition failed", "The progress changed since you read it. Read it again.");
      }
      const patch = body as OnboardingPatch;
      const unhide = new Set(patch.unhide_samples ?? []);
      if (new Set([...(current.hidden_samples ?? []), ...(patch.hide_samples ?? [])].filter((x) => !unhide.has(x))).size > 8) {
        return problem(422, "SCHEMA_INVALID", "Invalid request", "you can hide at most 8 sample projects", [{ code: "SCHEMA_INVALID", message: "Hide at most 8 sample projects.", pointer: "/hide_samples", severity: "error" }]);
      }
      const next = applyOnboardingPatch(current, patch);
      state.progress.set(`${p.id}/${tenant}`, next);
      save(state);
      return withEtag(next);
    }),
    http.post(RESET_RE, ({ request }) => {
      const [tenant] = groups(RESET_RE, request.url);
      const p = member(tenant);
      if (p instanceof Response) return p;
      const current = progressOf(state, p, tenant);
      const next: OnboardingState = { ...emptyOnboarding(), version: (current.version ?? 0) + 1, hidden_samples: current.hidden_samples ?? [], welcomed_at: current.welcomed_at ?? null };
      state.progress.set(`${p.id}/${tenant}`, next);
      save(state);
      return withEtag(next);
    }),

    // ------------------------------------------------------------------ samples
    http.get(`${base}/samples`, ({ params }) => {
      const tenant = String(params.tenant);
      const p = member(tenant);
      if (p instanceof Response) return p;
      if (samplesOff()) return HttpResponse.json([]);
      advance(tenant);
      return HttpResponse.json([sampleInfo(state, p, tenant, () => deps.session?.(tenant) ?? null)]);
    }),
    http.post(SAMPLE_ACTION_RE("install"), async ({ request }) => {
      const [tenant, sampleId] = groups(SAMPLE_ACTION_RE("install"), request.url);
      const p = member(tenant);
      if (p instanceof Response) return p;
      if (samplesOff()) return problem(404, "SAMPLES_DISABLED", "Samples are turned off", "Sample projects are turned off on this server.");
      if (sampleId !== SAMPLE_ID) return problem(404, "NOT_FOUND", "Not found", "There is no sample with this id.");
      await delay(150);
      const rec = state.samples.get(tenant);
      if (rec && !rec.retired) {
        // join: a membership of the sample project (at most runner); no second fit
        return HttpResponse.json(sampleInfo(state, p, tenant, () => deps.session?.(tenant) ?? null), { status: 200 });
      }
      if (rank(roleInTenant(p, tenant)) < rank("editor")) {
        return problem(403, "FORBIDDEN", "Not allowed", "Installing a sample starts a fit. You need the editor role somewhere in this tenant.");
      }
      state.samples.set(tenant, { installedAt: Date.now(), installer: p.id, attempts: 1, retired: false, seeded: false });
      deps.addProject(tenant, SAMPLE_PROJECT, "Sample: marketing mix");
      save(state);
      return HttpResponse.json(sampleInfo(state, p, tenant, () => deps.session?.(tenant) ?? null), { status: 202 });
    }),
    http.post(SAMPLE_ACTION_RE("retry"), ({ request }) => {
      const [tenant, sampleId] = groups(SAMPLE_ACTION_RE("retry"), request.url);
      const p = member(tenant);
      if (p instanceof Response) return p;
      if (samplesOff()) return problem(404, "SAMPLES_DISABLED", "Samples are turned off", "Sample projects are turned off on this server.");
      const rec = sampleId === SAMPLE_ID ? state.samples.get(tenant) : undefined;
      if (!rec) return problem(404, "NOT_FOUND", "Not found", "The sample is not installed.");
      if (rec.installer !== p.id && !isTenantAdmin(p, tenant)) {
        return problem(403, "FORBIDDEN", "Not allowed", "Only the person who installed the sample or a tenant admin can start it again.");
      }
      if (sampleStage(rec).state !== "failed") return problem(409, "CONFLICT", "Conflict", "The sample did not fail, so there is nothing to start again.");
      if (rec.attempts >= MAX_ATTEMPTS) return problem(409, "CONFLICT", "Conflict", `The sample used all ${MAX_ATTEMPTS} attempts.`);
      rec.attempts += 1;
      rec.installedAt = Date.now();
      save(state);
      return HttpResponse.json(sampleInfo(state, p, tenant, () => deps.session?.(tenant) ?? null), { status: 202 });
    }),
    http.post(SAMPLE_ACTION_RE("retire"), ({ request }) => {
      const [tenant, sampleId] = groups(SAMPLE_ACTION_RE("retire"), request.url);
      const p = member(tenant);
      if (p instanceof Response) return p;
      if (samplesOff()) return problem(404, "SAMPLES_DISABLED", "Samples are turned off", "Sample projects are turned off on this server.");
      const rec = sampleId === SAMPLE_ID ? state.samples.get(tenant) : undefined;
      if (!rec) return problem(404, "NOT_FOUND", "Not found", "The sample is not installed.");
      if (!isTenantAdmin(p, tenant)) return problem(403, "FORBIDDEN", "Not allowed", "Only a tenant admin can retire a sample.");
      rec.retired = true;
      save(state);
      return HttpResponse.json(sampleInfo(state, p, tenant, () => deps.session?.(tenant) ?? null));
    }),
  ];
}

/** Samples installed before a reload: their projects join the tenancy mock again. */
export function restoreSampleProjects(state: OnboardingMockState, addProject: OnboardingDeps["addProject"]): void {
  for (const tenant of state.samples.keys()) addProject(tenant, SAMPLE_PROJECT, "Sample: marketing mix");
}
