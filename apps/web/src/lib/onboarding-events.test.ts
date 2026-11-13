import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it, vi } from "vitest";

import { emitOnboarding, isOnboardingEvent, ONBOARDING_EVENT_TYPE, ONBOARDING_EVENTS, onOnboarding, type OnboardingMessage } from "./onboarding-events";

describe("onboarding events", () => {
  it("delivers an event and its detail to every listener", () => {
    const a = vi.fn<(m: OnboardingMessage) => void>();
    const b = vi.fn<(m: OnboardingMessage) => void>();
    const offA = onOnboarding(a);
    const offB = onOnboarding(b);
    emitOnboarding("fit_succeeded", { run_id: "run_1" });
    expect(a).toHaveBeenCalledWith({ event: "fit_succeeded", detail: { run_id: "run_1" } });
    expect(b).toHaveBeenCalledTimes(1);
    offA();
    offB();
  });

  it("stops after the cleanup", () => {
    const cb = vi.fn();
    const off = onOnboarding(cb);
    off();
    emitOnboarding("whatif_moved");
    expect(cb).not.toHaveBeenCalled();
  });

  it("ignores names it does not know, from either side", () => {
    const cb = vi.fn();
    const off = onOnboarding(cb);
    emitOnboarding("not_an_event" as never);
    window.dispatchEvent(new CustomEvent(ONBOARDING_EVENT_TYPE, { detail: { event: "rm -rf" } }));
    window.dispatchEvent(new CustomEvent(ONBOARDING_EVENT_TYPE, { detail: "report_viewed" }));
    window.dispatchEvent(new CustomEvent(ONBOARDING_EVENT_TYPE));
    expect(cb).not.toHaveBeenCalled();
    window.dispatchEvent(new CustomEvent(ONBOARDING_EVENT_TYPE, { detail: { event: "report_viewed" } }));
    expect(cb).toHaveBeenCalledWith({ event: "report_viewed", detail: {} });
    off();
  });

  it("never throws when a listener throws", () => {
    const off = onOnboarding(() => {
      throw new Error("boom");
    });
    // jsdom reports listener errors on window.onerror; the emitter itself must not throw
    const onError = (e: ErrorEvent) => e.preventDefault();
    window.addEventListener("error", onError);
    expect(() => emitOnboarding("tour_finished")).not.toThrow();
    window.removeEventListener("error", onError);
    off();
  });

  it("names the I-8 events exactly", () => {
    expect([...ONBOARDING_EVENTS].sort()).toEqual(
      ["dashboard_created", "dashboard_published", "dataset_uploaded", "fit_succeeded", "library_item_saved", "recipe_tried", "report_viewed", "sample_opened", "tour_finished", "whatif_moved"].sort(),
    );
    expect(isOnboardingEvent("sample_opened")).toBe(true);
    expect(isOnboardingEvent("SAMPLE_OPENED")).toBe(false);
  });

  it("imports nothing (safe in any signed-in chunk)", () => {
    const src = readFileSync(resolve(__dirname, "onboarding-events.ts"), "utf8");
    expect(src).not.toMatch(/^\s*import\s/m);
    expect(src).not.toMatch(/\bfrom\s+["']/);
  });
});
