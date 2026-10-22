import { afterEach, describe, expect, it } from "vitest";

import { emptyOnboarding, type OnboardingState } from "./api";
import {
  isFreshProgress,
  NO_TOUR_KEY,
  noTourFlag,
  readPosition,
  resumablePosition,
  shouldWelcome,
  startTour,
  TOUR_POSITION_KEY,
  tourParam,
  tourPatch,
  useTourStore,
  withoutTourParam,
  type WelcomeInput,
} from "./use-onboarding";

const fresh = emptyOnboarding();
const base: WelcomeInput = { state: fresh, pathname: "/", search: "", noTour: false, suppressed: false, tourActive: false };

afterEach(() => {
  useTourStore.setState({ active: null, suppressed: false, returnFocus: null });
});

describe("shouldWelcome (D24 polite auto-start)", () => {
  it("welcomes a first visit with no progress", () => {
    expect(shouldWelcome(base)).toBe(true);
  });

  it("stays quiet with tips off, once welcomed, or with any progress", () => {
    expect(shouldWelcome({ ...base, state: { ...fresh, tips_off: true } })).toBe(false);
    expect(shouldWelcome({ ...base, state: { ...fresh, welcomed_at: "2026-10-01T09:00:00Z" } })).toBe(false);
    expect(shouldWelcome({ ...base, state: { ...fresh, checklist: { own_data: "2026-10-01T09:00:00Z" } } })).toBe(false);
    const tours = { welcome: { status: "dismissed", step: 1, updated_at: "2026-10-01T09:00:00Z" } } as OnboardingState["tours"];
    expect(shouldWelcome({ ...base, state: { ...fresh, tours } })).toBe(false);
    expect(shouldWelcome({ ...base, state: { ...fresh, checklist_dismissed: true } })).toBe(false);
  });

  it("never on /admin, with ?tour=off or a deep link, with mapi.noTour, mid-tour, or before progress loads", () => {
    expect(shouldWelcome({ ...base, pathname: "/admin" })).toBe(false);
    expect(shouldWelcome({ ...base, pathname: "/admin/tenants" })).toBe(false);
    expect(shouldWelcome({ ...base, pathname: "/administer" })).toBe(true);
    expect(shouldWelcome({ ...base, search: "?tour=off" })).toBe(false);
    expect(shouldWelcome({ ...base, search: "?tour=whatif" })).toBe(false);
    expect(shouldWelcome({ ...base, suppressed: true })).toBe(false);
    expect(shouldWelcome({ ...base, noTour: true })).toBe(false);
    expect(shouldWelcome({ ...base, tourActive: true })).toBe(false);
    expect(shouldWelcome({ ...base, state: undefined })).toBe(false);
  });

  it("reads the mapi.noTour switch from localStorage", () => {
    expect(noTourFlag()).toBe(false);
    localStorage.setItem(NO_TOUR_KEY, "1");
    expect(noTourFlag()).toBe(true);
  });

  it("isFreshProgress is false once anything is recorded", () => {
    expect(isFreshProgress(fresh)).toBe(true);
    expect(isFreshProgress({ ...fresh, welcomed_at: "2026-10-01T09:00:00Z" })).toBe(false);
  });
});

describe("tour deep links", () => {
  it("parses ?tour=<id> and ?tour=off and ignores unknown ids", () => {
    expect(tourParam("?tour=whatif")).toBe("whatif");
    expect(tourParam("tour=off")).toBe("off");
    expect(tourParam("?tour=nope")).toBeNull();
    expect(tourParam("?tab=plots")).toBeNull();
  });

  it("drops only the tour parameter", () => {
    expect(withoutTourParam("?tab=plots&tour=whatif")).toBe("?tab=plots");
    expect(withoutTourParam("?tour=off")).toBe("");
  });
});

describe("the tour store and position", () => {
  it("keeps the running position in localStorage and clears it at the end", () => {
    startTour("welcome", 2);
    expect(useTourStore.getState().active).toEqual({ id: "welcome", step: 2 });
    expect(readPosition()).toEqual({ id: "welcome", step: 2 });
    useTourStore.getState().go(3);
    expect(readPosition()).toEqual({ id: "welcome", step: 3 });
    useTourStore.getState().stop();
    expect(useTourStore.getState().active).toBeNull();
    expect(localStorage.getItem(TOUR_POSITION_KEY)).toBeNull();
  });

  it("clamps steps and ignores a bad saved position", () => {
    startTour("data", 999);
    expect(useTourStore.getState().active?.step).toBe(50);
    localStorage.setItem(TOUR_POSITION_KEY, '{"id":"nope","step":1}');
    expect(readPosition()).toBeNull();
    localStorage.setItem(TOUR_POSITION_KEY, "not json");
    expect(readPosition()).toBeNull();
  });

  it("gives focus back to the control that started the tour", () => {
    const button = document.createElement("button");
    document.body.append(button);
    button.focus();
    startTour("welcome");
    const other = document.createElement("button");
    document.body.append(other);
    other.focus();
    useTourStore.getState().stop();
    expect(document.activeElement).toBe(button);
    button.remove();
    other.remove();
  });

  it("resumes only a tour the server has not finished or skipped, and never when tours are off", () => {
    const saved = { id: "whatif", step: 1 } as const;
    const opts = { noTour: false, suppressed: false, pathname: "/" };
    expect(resumablePosition(saved, fresh, opts)).toEqual(saved);
    const done = { ...fresh, tours: { whatif: { status: "completed", step: 2, updated_at: "2026-10-01T09:00:00Z" } } } as OnboardingState;
    expect(resumablePosition(saved, done, opts)).toBeNull();
    expect(resumablePosition(saved, fresh, { ...opts, noTour: true })).toBeNull();
    expect(resumablePosition(saved, fresh, { ...opts, suppressed: true })).toBeNull();
    expect(resumablePosition(saved, fresh, { ...opts, pathname: "/admin" })).toBeNull();
    expect(resumablePosition(null, fresh, opts)).toBeNull();
  });

  it("builds the progress PATCH body", () => {
    const body = tourPatch("whatif", "in_progress", 2);
    expect(body.tours?.whatif).toMatchObject({ status: "in_progress", step: 2 });
  });
});
