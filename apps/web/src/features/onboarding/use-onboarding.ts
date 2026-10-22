/**
 * Which tour is playing, and when the app may offer one (v0.18 D18, D19, D24).
 *
 * - **The tour store** (`useTourStore`) holds the running tour and step. Any signed-in chunk
 *   may start a tour with {@link startTour} (the Start-here hub, the user menu, Settings)
 *   without loading the tour host: the host is a lazy chunk the shell mounts, and it plays
 *   whatever the store says.
 * - **The position** of a running tour is kept in localStorage (`mapi.tour`) so a reload
 *   resumes it. That is the only tour state in the browser: progress (which tours were
 *   finished or skipped, and the step) goes to the server ({@link useTourReporter}).
 * - **Polite auto-start** ({@link shouldWelcome}): the welcome dialog shows on the first
 *   signed-in visit in a tenant with no progress, never with tips off, on `/admin`, with
 *   `?tour=off`, or when localStorage `mapi.noTour=1` (the `@e2e` storage state sets it).
 *   `?tour=<id>` opens a tour directly.
 *
 * Progress is UX state, not a security control. Share-link and client-portal code never
 * imports this module.
 */
import { useCallback, useRef } from "react";
import { create } from "zustand";

import { emitOnboarding } from "@/lib/onboarding-events";

import { MAX_TOUR_STEP, tourProgress, usePatchOnboarding, useOnboardingTenant, type OnboardingPatch, type OnboardingState, type TourId, type TourStatus } from "./api";
import { isTourId } from "./tours";

/** localStorage: `"1"` turns off every automatic tour and the welcome dialog (deep links still work). */
export const NO_TOUR_KEY = "mapi.noTour";
/** localStorage: the running tour's position, `{"id":"welcome","step":2}`. */
export const TOUR_POSITION_KEY = "mapi.tour";
/** The query parameter of a tour deep link (`?tour=whatif`, or `?tour=off`). */
export const TOUR_PARAM = "tour";

export interface TourPosition {
  id: TourId;
  step: number;
}

// ------------------------------------------------------------------------------ storage

function readStorage(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeStorage(key: string, value: string | null): void {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    // private mode or blocked storage: the tour still plays, it just does not resume
  }
}

/** True when this browser asked for no automatic tours (`mapi.noTour=1`). */
export function noTourFlag(): boolean {
  return readStorage(NO_TOUR_KEY) === "1";
}

function clampStep(step: number): number {
  return Number.isFinite(step) ? Math.max(0, Math.min(MAX_TOUR_STEP, Math.trunc(step))) : 0;
}

/** The saved position, or `null` when there is none or it does not parse. */
export function readPosition(): TourPosition | null {
  const raw = readStorage(TOUR_POSITION_KEY);
  if (!raw) return null;
  try {
    const v = JSON.parse(raw) as { id?: unknown; step?: unknown };
    if (!isTourId(v.id) || typeof v.step !== "number") return null;
    return { id: v.id, step: clampStep(v.step) };
  } catch {
    return null;
  }
}

function savePosition(pos: TourPosition | null): void {
  writeStorage(TOUR_POSITION_KEY, pos ? JSON.stringify(pos) : null);
}

// ------------------------------------------------------------------------------ store

export interface TourStore {
  active: TourPosition | null;
  /** `?tour=off` was in the URL during this page load: nothing starts by itself. */
  suppressed: boolean;
  /** Where focus goes back to when the tour ends (the button that started it). */
  returnFocus: HTMLElement | null;
  start: (id: TourId, step?: number) => void;
  go: (step: number) => void;
  /** End the tour (the caller reports finish or skip first) and give focus back. */
  stop: () => void;
  suppress: () => void;
}

function focusedElement(): HTMLElement | null {
  const el = typeof document === "undefined" ? null : document.activeElement;
  return el instanceof HTMLElement && el !== document.body ? el : null;
}

export const useTourStore = create<TourStore>()((set, get) => ({
  active: null,
  suppressed: false,
  returnFocus: null,
  start: (id, step = 0) => {
    const active = { id, step: clampStep(step) };
    savePosition(active);
    set({ active, returnFocus: get().active ? get().returnFocus : focusedElement() });
  },
  go: (step) => {
    const cur = get().active;
    if (!cur) return;
    const active = { id: cur.id, step: clampStep(step) };
    savePosition(active);
    set({ active });
  },
  stop: () => {
    const back = get().returnFocus;
    savePosition(null);
    set({ active: null, returnFocus: null });
    if (back?.isConnected) back.focus();
  },
  suppress: () => set({ suppressed: true }),
}));

/** Start (or restart) tour `id`. Safe to call from any signed-in chunk; the shell's tour host plays it. */
export function startTour(id: TourId, step = 0): void {
  useTourStore.getState().start(id, step);
}

/** The running tour, or `null`. */
export function useActiveTour(): TourPosition | null {
  return useTourStore((s) => s.active);
}

// ------------------------------------------------------------------------------ auto-start

/** The `tour` query parameter: a tour id, `"off"`, or `null` (absent or unknown). */
export function tourParam(search: string): TourId | "off" | null {
  const v = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search).get(TOUR_PARAM);
  if (v === "off") return "off";
  return isTourId(v) ? v : null;
}

/** `search` without the `tour` parameter (`""` when nothing is left). */
export function withoutTourParam(search: string): string {
  const params = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
  params.delete(TOUR_PARAM);
  const rest = params.toString();
  return rest ? `?${rest}` : "";
}

/** Tours, the welcome dialog and resuming never start by themselves on the operator console. */
export function isQuietPath(pathname: string): boolean {
  return pathname === "/admin" || pathname.startsWith("/admin/");
}

/** True when the user has made no progress at all in this tenant (a first visit). */
export function isFreshProgress(state: OnboardingState): boolean {
  return (
    !state.welcomed_at &&
    Object.keys(state.checklist ?? {}).length === 0 &&
    Object.keys(state.tours ?? {}).length === 0 &&
    !state.checklist_dismissed
  );
}

export interface WelcomeInput {
  /** The user's progress; `undefined` while it loads or without a signed-in session. */
  state: OnboardingState | undefined;
  pathname: string;
  search: string;
  /** localStorage `mapi.noTour=1`. */
  noTour: boolean;
  /** `?tour=off` was seen during this page load. */
  suppressed: boolean;
  tourActive: boolean;
}

/** D24: show the welcome dialog now? */
export function shouldWelcome(i: WelcomeInput): boolean {
  if (!i.state || i.noTour || i.suppressed || i.tourActive) return false;
  if (isQuietPath(i.pathname) || tourParam(i.search) !== null) return false;
  if (i.state.tips_off) return false;
  return isFreshProgress(i.state);
}

/**
 * The saved position to resume after a reload, or `null`: only a tour the server does not
 * record as finished or skipped, and never when automatic tours are off.
 */
export function resumablePosition(
  saved: TourPosition | null,
  state: OnboardingState | undefined,
  opts: { noTour: boolean; suppressed: boolean; pathname: string },
): TourPosition | null {
  if (!saved || opts.noTour || opts.suppressed || isQuietPath(opts.pathname)) return null;
  const status = state?.tours?.[saved.id]?.status;
  if (status === "completed" || status === "dismissed") return null;
  return saved;
}

// ------------------------------------------------------------------------------ progress

/** The PATCH body that records tour `id` at `step` with `status`. */
export function tourPatch(id: TourId, status: TourStatus, step: number): OnboardingPatch {
  return { tours: { [id]: tourProgress(status, step) } };
}

export interface TourReporter {
  /** The tour moved to `step` (sent once per step). */
  step: (id: TourId, step: number) => void;
  /** The last step's "Finish": the server ticks "took_tour"; the checklist hears `tour_finished`. */
  finish: (id: TourId, step: number) => void;
  /** "Skip the tour" or Esc. */
  skip: (id: TourId, step: number) => void;
}

/** Sends tour progress to the server (D19). Idle without a signed-in tenant. */
export function useTourReporter(state: OnboardingState | undefined): TourReporter {
  const tenant = useOnboardingTenant();
  const patch = usePatchOnboarding();
  const mutate = patch.mutate;
  const last = useRef<string>("");
  const stateRef = useRef(state);
  stateRef.current = state;

  const send = useCallback(
    (id: TourId, status: TourStatus, step: number) => {
      if (!tenant) return;
      const key = `${id}:${status}:${step}`;
      if (last.current === key) return;
      const known = stateRef.current?.tours?.[id];
      // a reload resumes at the step the server already has: nothing to send
      if (status === "in_progress" && known?.status === "in_progress" && (known.step ?? 0) === step && last.current === "") {
        last.current = key;
        return;
      }
      last.current = key;
      mutate({ patch: tourPatch(id, status, step) });
    },
    [tenant, mutate],
  );

  return {
    step: useCallback((id: TourId, step: number) => send(id, "in_progress", step), [send]),
    finish: useCallback(
      (id: TourId, step: number) => {
        send(id, "completed", step);
        emitOnboarding("tour_finished", { tour: id });
      },
      [send],
    ),
    skip: useCallback((id: TourId, step: number) => send(id, "dismissed", step), [send]),
  };
}
