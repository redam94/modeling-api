/**
 * Onboarding events (v0.18 D25): a tiny `CustomEvent` channel between the app and the
 * first-run checklist. Any signed-in chunk may call {@link emitOnboarding} ("a fit
 * finished", "the user moved a what-if slider"); `features/onboarding` listens with
 * {@link onOnboarding} and records progress on the server. The emitter knows nothing about
 * the checklist, so pages stay decoupled from the onboarding chunk.
 *
 * This module has no imports and no side effects at load (a few hundred bytes), so it is
 * safe in every signed-in chunk. Share-link and client-portal code never imports it.
 * Progress is UX state, not a security control: a forged event only ticks a box.
 */

/** Everything the checklist and the tours listen for. */
export const ONBOARDING_EVENTS = [
  "dataset_uploaded",
  "fit_succeeded",
  "report_viewed",
  "whatif_moved",
  "dashboard_created",
  "dashboard_published",
  "recipe_tried",
  "sample_opened",
  "library_item_saved",
  "tour_finished",
] as const;

export type OnboardingEvent = (typeof ONBOARDING_EVENTS)[number];

/** Optional context of an event (ids only; never user-authored text). */
export type OnboardingDetail = Readonly<Record<string, string | number | boolean | null>>;

export interface OnboardingMessage {
  event: OnboardingEvent;
  detail: OnboardingDetail;
}

/** The DOM event type on `window`. */
export const ONBOARDING_EVENT_TYPE = "mapi:onboarding";

const KNOWN: ReadonlySet<string> = new Set(ONBOARDING_EVENTS);

export function isOnboardingEvent(value: unknown): value is OnboardingEvent {
  return typeof value === "string" && KNOWN.has(value);
}

function target(): EventTarget | null {
  return typeof window === "undefined" ? null : window;
}

/** Tell the onboarding listeners that `event` happened. Never throws. */
export function emitOnboarding(event: OnboardingEvent, detail: OnboardingDetail = {}): void {
  const t = target();
  if (!t || !isOnboardingEvent(event)) return;
  try {
    const message: OnboardingMessage = { event, detail: { ...detail } };
    t.dispatchEvent(new CustomEvent<OnboardingMessage>(ONBOARDING_EVENT_TYPE, { detail: message }));
  } catch {
    // a listener threw or CustomEvent is missing: progress is best effort
  }
}

/**
 * Call `cb` for every onboarding event (unknown names are ignored). Returns the function
 * that stops listening (use it as a React effect cleanup).
 */
export function onOnboarding(cb: (message: OnboardingMessage) => void): () => void {
  const t = target();
  if (!t) return () => undefined;
  const listener = (e: Event) => {
    const d = (e as CustomEvent<unknown>).detail;
    if (typeof d !== "object" || d === null) return;
    const m = d as { event?: unknown; detail?: unknown };
    if (!isOnboardingEvent(m.event)) return;
    const detail = typeof m.detail === "object" && m.detail !== null ? (m.detail as OnboardingDetail) : {};
    cb({ event: m.event, detail });
  };
  t.addEventListener(ONBOARDING_EVENT_TYPE, listener);
  return () => t.removeEventListener(ONBOARDING_EVENT_TYPE, listener);
}
