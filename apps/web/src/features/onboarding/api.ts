/**
 * Onboarding data (v0.18 D19-D20, I-3, I-5): the signed-in user's checklist and tour
 * progress in the selected tenant (`GET/PATCH /v1/tenants/{t}/me/onboarding`, `:reset`) and
 * the tenant's sample projects (`GET /v1/tenants/{t}/samples`, `:install`, `:retry`,
 * `:retire`).
 *
 * Progress is UX state, kept server-side so it follows the user between browsers. A PATCH
 * only ever adds (a completed step never reverts), so the hooks update the cache at once
 * (optimistic, with {@link applyOnboardingPatch}, the mirror of `mapi_spec.onboarding
 * .apply_patch`) and send without `If-Match`; the server's answer replaces the guess.
 *
 * Only a signed-in session has progress: with no session (the v0.2 single-tenant profile)
 * every hook here stays idle. Share-link and client-portal code never imports this module.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import { api, ApiError, unwrap, unwrapWithStatus } from "@/api/client";
import { qk, useProjectScope } from "@/api/queries";
import type { components } from "@/api/schema";
import { onOnboarding, type OnboardingEvent } from "@/lib/onboarding-events";
import { useAuth } from "@/stores/auth";

import { ok } from "./keys";

type Schemas = components["schemas"];

export type OnboardingState = Schemas["OnboardingState"];
export type OnboardingPatch = Schemas["OnboardingPatch"];
export type TourProgress = Schemas["TourProgress"];
export type TourStatus = TourProgress["status"];
export type SampleInfo = Schemas["SampleInfo"];
export type SampleState = NonNullable<SampleInfo["state"]>;
export type ChecklistStep = NonNullable<OnboardingPatch["complete"]>[number];

/** The checklist in the order the Start-here hub shows it (`mapi_spec.onboarding.ChecklistStep`). */
export const CHECKLIST_STEPS = [
  "took_tour",
  "open_sample",
  "fit_model",
  "read_results",
  "try_whatif",
  "make_dashboard",
  "own_data",
  "make_your_own",
] as const satisfies readonly ChecklistStep[];

/** The tours (`mapi_spec.onboarding.TourId`); the web holds their steps. */
export const TOUR_IDS = ["welcome", "data", "fit", "results", "whatif", "dashboard", "make_your_own"] as const;
export type TourId = (typeof TOUR_IDS)[number];

export const SAMPLE_STATES = ["provisioning", "ingesting", "fitting", "ready", "failed", "retired"] as const satisfies readonly SampleState[];
/** States in which the sample loop is still working (poll while one is shown). */
export const ACTIVE_SAMPLE_STATES: ReadonlySet<SampleState> = new Set(["provisioning", "ingesting", "fitting"]);

export const MAX_TOUR_STEP = 50;
export const MAX_HIDDEN_SAMPLES = 8;
/** How often the sample list is read again while a sample is being made. */
export const SAMPLE_POLL_MS = 1500;

/** Which checklist step each onboarding event completes (`null`: the event ticks nothing). */
export const EVENT_STEP: Record<OnboardingEvent, ChecklistStep | null> = {
  dataset_uploaded: "own_data",
  fit_succeeded: "fit_model",
  report_viewed: "read_results",
  whatif_moved: "try_whatif",
  dashboard_created: "make_dashboard",
  dashboard_published: "make_dashboard",
  recipe_tried: null,
  sample_opened: "open_sample",
  library_item_saved: "make_your_own",
  tour_finished: "took_tour",
};

// ------------------------------------------------------------------------------ pure helpers

/** The state before the first change (what a fresh user reads). */
export function emptyOnboarding(): OnboardingState {
  return { version: 0, checklist: {}, tours: {}, checklist_dismissed: false, tips_off: false, hidden_samples: [], welcomed_at: null };
}

/** True when every step is done or the user dismissed the checklist (the hub then collapses). */
export function checklistDone(state: OnboardingState | undefined): boolean {
  if (!state) return false;
  if (state.checklist_dismissed) return true;
  const done = state.checklist ?? {};
  return CHECKLIST_STEPS.every((s) => s in done);
}

/** How many checklist steps are done. */
export function checklistCount(state: OnboardingState | undefined): number {
  const done = state?.checklist ?? {};
  return CHECKLIST_STEPS.filter((s) => s in done).length;
}

export function isStepDone(state: OnboardingState | undefined, step: ChecklistStep): boolean {
  return Boolean(state?.checklist && step in state.checklist);
}

/**
 * Merge `patch` into `state` like the server does (`mapi_spec.onboarding.apply_patch`): steps
 * are only added (a done step keeps its first time), named tours are replaced and a
 * completed tour completes `took_tour`, unhide wins over hide, `welcomed: true` records the
 * first welcome once. `version` goes up by one when anything changed.
 */
export function applyOnboardingPatch(state: OnboardingState, patch: OnboardingPatch, now: string = new Date().toISOString()): OnboardingState {
  const checklist: Record<string, string> = { ...(state.checklist ?? {}) };
  for (const step of patch.complete ?? []) checklist[step] ??= now;
  const tours: Record<string, TourProgress> = { ...(state.tours ?? {}) };
  for (const [id, progress] of Object.entries(patch.tours ?? {})) {
    tours[id] = { ...progress, updated_at: now };
    if (progress.status === "completed") checklist.took_tour ??= now;
  }
  const unhide = new Set(patch.unhide_samples ?? []);
  const hidden = [...(state.hidden_samples ?? [])];
  for (const s of patch.hide_samples ?? []) if (!hidden.includes(s)) hidden.push(s);
  const next: OnboardingState = {
    ...state,
    checklist: Object.fromEntries(CHECKLIST_STEPS.filter((s) => s in checklist).map((s) => [s, checklist[s] ?? now])),
    tours,
    checklist_dismissed: patch.checklist_dismissed ?? state.checklist_dismissed ?? false,
    tips_off: patch.tips_off ?? state.tips_off ?? false,
    hidden_samples: hidden.filter((s) => !unhide.has(s)).slice(0, MAX_HIDDEN_SAMPLES),
    welcomed_at: state.welcomed_at ?? (patch.welcomed ? now : null),
  };
  return canonical(next) === canonical(state) ? state : { ...next, version: (state.version ?? 0) + 1 };
}

/** The state as sorted JSON with defaults filled and `version` left out (for "did it change"). */
function canonical(state: OnboardingState): string {
  const s = { ...emptyOnboarding(), ...state };
  const sortObj = <T,>(o: Record<string, T> | undefined) => Object.fromEntries(Object.entries(o ?? {}).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)));
  return JSON.stringify([
    sortObj(s.checklist),
    Object.entries(sortObj(s.tours)).map(([k, t]) => [k, t.status, t.step ?? 0, t.updated_at]),
    Boolean(s.checklist_dismissed),
    Boolean(s.tips_off),
    s.hidden_samples ?? [],
    s.welcomed_at ?? null,
  ]);
}

/** The steps of `steps` that are not done yet (an empty list means "nothing to send"). */
export function missingSteps(state: OnboardingState | undefined, steps: readonly ChecklistStep[]): ChecklistStep[] {
  return [...new Set(steps)].filter((s) => !isStepDone(state, s));
}

/** A tour progress entry to PATCH (the server sets `updated_at`). */
export function tourProgress(status: TourStatus, step = 0): TourProgress {
  return { status, step: Math.max(0, Math.min(MAX_TOUR_STEP, Math.trunc(step))), updated_at: new Date().toISOString() };
}

export function isSampleActive(info: Pick<SampleInfo, "state"> | undefined): boolean {
  return Boolean(info?.state && ACTIVE_SAMPLE_STATES.has(info.state));
}

/** Where a sample is on its way to `ready`, 0..1 (the fit's own progress inside `fitting`). */
export function sampleFraction(info: Pick<SampleInfo, "state" | "progress"> | undefined): number {
  switch (info?.state) {
    case "provisioning":
      return 0.05;
    case "ingesting":
      return 0.15;
    case "fitting":
      return 0.2 + 0.78 * Math.max(0, Math.min(1, info.progress ?? 0));
    case "ready":
      return 1;
    default:
      return 0;
  }
}

/** The sample route to open once it is ready (the model version's report), else `null`. */
export function sampleModelPath(info: SampleInfo | undefined): string | null {
  if (!info || info.state !== "ready" || !info.model_id || info.model_version == null) return null;
  return `/models/${encodeURIComponent(info.model_id)}/versions/${info.model_version}`;
}

// ------------------------------------------------------------------------------ scope

/** The tenant whose onboarding the app shows: the active project's, in a signed-in session only. */
export function useOnboardingTenant(): string | null {
  const s = useProjectScope();
  const mode = useAuth((a) => a.mode);
  return mode === "session" && s.tenantId ? s.tenantId : null;
}

// ------------------------------------------------------------------------------ progress

function isStale(e: unknown): boolean {
  return e instanceof ApiError && (e.status === 412 || e.code === "PRECONDITION_FAILED");
}

/** The caller's checklist and tour progress (idle without a session). */
export function useOnboarding(opts: { enabled?: boolean } = {}) {
  const tenant = useOnboardingTenant();
  return useQuery({
    queryKey: ok.state(tenant ?? "-"),
    queryFn: ({ signal }) => unwrap(api.GET("/v1/tenants/{tenant_id}/me/onboarding", { params: { path: { tenant_id: tenant ?? "" } }, signal })),
    enabled: (opts.enabled ?? true) && tenant !== null,
    staleTime: 60_000,
    // progress gates nothing: a failure leaves the hub hidden rather than retrying loudly
    retry: (count, err) => err instanceof TypeError && count < 2,
  });
}

export interface PatchInput {
  patch: OnboardingPatch;
  /** Apply only to this version (`"<n>"`; 412 when it moved). Leave out for plain merges. */
  ifMatch?: string | null;
}

/** Merge progress (optimistic: the cache shows the change before the server answers). */
export function usePatchOnboarding() {
  const tenant = useOnboardingTenant();
  const qc = useQueryClient();
  const key = ok.state(tenant ?? "-");
  return useMutation({
    mutationFn: ({ patch, ifMatch }: PatchInput) => {
      if (!tenant) return Promise.reject(new Error("no signed-in tenant"));
      return unwrap(
        api.PATCH("/v1/tenants/{tenant_id}/me/onboarding", {
          params: { path: { tenant_id: tenant } },
          ...(ifMatch ? { headers: { "If-Match": ifMatch } } : {}),
          body: patch,
        }),
      );
    },
    onMutate: async ({ patch }) => {
      await qc.cancelQueries({ queryKey: key });
      const prev = qc.getQueryData<OnboardingState>(key);
      if (prev) qc.setQueryData(key, applyOnboardingPatch(prev, patch));
      return { prev };
    },
    onSuccess: (state) => qc.setQueryData(key, state),
    onError: async (e, _vars, ctx) => {
      if (ctx?.prev) qc.setQueryData(key, ctx.prev);
      if (isStale(e)) await qc.invalidateQueries({ queryKey: key });
    },
  });
}

/** Start the checklist and the tours again (Settings › "Reset tips"). */
export function useResetOnboarding() {
  const tenant = useOnboardingTenant();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => unwrap(api.POST("/v1/tenants/{tenant_id}/me/onboarding:reset", { params: { path: { tenant_id: tenant ?? "" } } })),
    onSuccess: (state) => qc.setQueryData(ok.state(tenant ?? "-"), state),
  });
}

/** Mark checklist steps done (sends nothing when they already are). */
export function useCompleteSteps() {
  const tenant = useOnboardingTenant();
  const qc = useQueryClient();
  const patch = usePatchOnboarding();
  return (steps: readonly ChecklistStep[]) => {
    if (!tenant) return;
    const state = qc.getQueryData<OnboardingState>(ok.state(tenant));
    // before the first read the server merges anyway; after it, skip what is done
    const missing = state ? missingSteps(state, steps) : [...new Set(steps)];
    if (missing.length) patch.mutate({ patch: { complete: missing } });
  };
}

/**
 * Tick checklist steps from onboarding events (D25): mount once in the signed-in shell (the
 * tour host or the hub). Each step is sent at most once per mount; unknown events are ignored.
 */
export function useOnboardingEventSync(enabled = true) {
  const complete = useCompleteSteps();
  const completeRef = useRef(complete);
  useEffect(() => {
    completeRef.current = complete;
  });
  useEffect(() => {
    if (!enabled) return undefined;
    const sent = new Set<ChecklistStep>();
    return onOnboarding(({ event }) => {
      const step = EVENT_STEP[event];
      if (!step || sent.has(step)) return;
      sent.add(step);
      completeRef.current([step]);
    });
  }, [enabled]);
}

// ------------------------------------------------------------------------------ samples

/** The tenant's sample projects (`[]` when samples are off); polls while one is being made. */
export function useSamples(opts: { enabled?: boolean } = {}) {
  const tenant = useOnboardingTenant();
  return useQuery({
    queryKey: ok.samples(tenant ?? "-"),
    queryFn: ({ signal }) => unwrap(api.GET("/v1/tenants/{tenant_id}/samples", { params: { path: { tenant_id: tenant ?? "" } }, signal })),
    enabled: (opts.enabled ?? true) && tenant !== null,
    refetchInterval: (q) => (q.state.data?.some((s) => isSampleActive(s)) ? SAMPLE_POLL_MS : false),
    retry: (count, err) => err instanceof TypeError && count < 2,
  });
}

export type SampleAction = "install" | "retry" | "retire";

function sampleAction(tenant: string, id: string, action: SampleAction) {
  const params = { params: { path: { tenant_id: tenant, sample_id: id } } };
  if (action === "install") return unwrapWithStatus(api.POST("/v1/tenants/{tenant_id}/samples/{sample_id}:install", params));
  if (action === "retry") return unwrapWithStatus(api.POST("/v1/tenants/{tenant_id}/samples/{sample_id}:retry", params));
  return unwrapWithStatus(api.POST("/v1/tenants/{tenant_id}/samples/{sample_id}:retire", params));
}

export interface SampleActionResult {
  info: SampleInfo;
  /** `install` only: `true` when this call started the install (202), `false` when it joined (200). */
  installed: boolean;
}

/** Install (or join), retry or retire a sample. The sample project then shows in the project switcher. */
export function useSampleAction() {
  const tenant = useOnboardingTenant();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, action }: { id: string; action: SampleAction }): Promise<SampleActionResult> => {
      if (!tenant) throw new Error("no signed-in tenant");
      const { data, status } = await sampleAction(tenant, id, action);
      return { info: data, installed: action === "install" && status === 202 };
    },
    onSuccess: async ({ info }) => {
      const key = ok.samples(tenant ?? "-");
      qc.setQueryData<SampleInfo[]>(key, (prev) => (prev ? prev.map((s) => (s.id === info.id ? info : s)) : [info]));
      await Promise.all([
        qc.invalidateQueries({ queryKey: key }),
        qc.invalidateQueries({ queryKey: qk.me }),
        tenant ? qc.invalidateQueries({ queryKey: qk.tenantProjects(tenant) }) : Promise.resolve(),
      ]);
    },
  });
}
