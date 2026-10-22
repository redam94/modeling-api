import { describe, expect, it } from "vitest";

import { TOUR_IDS, type SampleInfo } from "./api";
import { isTourAnchor } from "./anchors";
import { isTourId, onRoute, sampleStepPath, stepRoute, tourAvailable, TOUR_ORDER, TOURS, tourSteps, type TourContext } from "./tours";

const ready: SampleInfo = {
  id: "geo_mmm",
  title: "Geo MMM",
  summary: "",
  minutes: 2,
  state: "ready",
  project_id: "sample",
  spec_id: "spec_123",
  run_id: "run_1",
  model_id: "eu-geo-mmm",
  model_version: 1,
  progress: 1,
  problem: null,
  attempts: 1,
  can_install: false,
  can_retry: false,
  can_retire: false,
  hidden: false,
};

const expert: TourContext = { persona: "expert", sample: null };
const guided: TourContext = { persona: "guided", sample: null };

describe("tours", () => {
  it("defines every server tour id, in hub order", () => {
    expect(Object.keys(TOURS).sort()).toEqual([...TOUR_IDS].sort());
    expect([...TOUR_ORDER].sort()).toEqual([...TOUR_IDS].sort());
    for (const id of TOUR_IDS) expect(TOURS[id].id).toBe(id);
    expect(isTourId("whatif")).toBe(true);
    expect(isTourId("toString")).toBe(false);
  });

  it("keeps step ids unique, anchors listed, copy short and within the 50-step cap", () => {
    for (const tour of Object.values(TOURS)) {
      const ids = tour.steps.map((s) => s.id);
      expect(new Set(ids).size).toBe(ids.length);
      expect(tour.steps.length).toBeLessThanOrEqual(50);
      for (const s of tour.steps) {
        expect(isTourAnchor(s.anchor)).toBe(true);
        expect(s.title.length).toBeLessThanOrEqual(60);
        expect(s.body.length).toBeLessThanOrEqual(200);
      }
    }
  });

  it("never promises an instant server result (probe P11: previews take seconds)", () => {
    const copy = Object.values(TOURS).flatMap((t) => t.steps.flatMap((s) => [s.title, s.body, s.note ?? ""]));
    expect(copy.filter((c) => /\binstant(ly)?\b/i.test(c))).toEqual([]);
  });

  it("the welcome tour follows the persona (Guided mode has no Expert Home)", () => {
    const e = tourSteps(TOURS.welcome, expert).map((s) => s.anchor);
    const g = tourSteps(TOURS.welcome, guided).map((s) => s.anchor);
    expect(e).toContain("home.start-here");
    expect(g).not.toContain("home.start-here");
    expect(g[0]).toBe("guided.templates");
    expect(e.at(-1)).toBe("shell.help-menu");
    expect(g.at(-1)).toBe("shell.help-menu");
  });

  it("sample tours need a ready sample and open its pages", () => {
    expect(tourAvailable(TOURS.whatif, expert)).toBe(false);
    expect(tourAvailable(TOURS.whatif, { ...expert, sample: { ...ready, state: "fitting" } })).toBe(false);
    expect(tourAvailable(TOURS.whatif, { ...expert, sample: ready })).toBe(true);
    expect(tourAvailable(TOURS.welcome, expert)).toBe(true);
    expect(tourAvailable(TOURS.make_your_own, expert)).toBe(true);

    const ctx = { ...expert, sample: ready };
    expect(sampleStepPath(ctx, "explore")).toBe("/guided/spec_123/explore");
    expect(sampleStepPath(expert, "explore")).toBeNull();
    expect(tourSteps(TOURS.whatif, ctx).map((s) => stepRoute(s, ctx))).toEqual([
      "/guided/spec_123/explore",
      "/guided/spec_123/explore",
      "/guided/spec_123/explore",
    ]);
    expect(stepRoute(TOURS.dashboard.steps[0]!, ctx)).toBe("/dashboards?new=1&model=eu-geo-mmm");
    // a step whose anchor the previous step makes appear stays on its page
    expect(stepRoute(TOURS.dashboard.steps[1]!, ctx)).toBeUndefined();
    // the fit tour starts on the persona's start page
    expect(stepRoute(TOURS.fit.steps[0]!, { ...guided, sample: ready })).toBe("/guided");
  });

  it("onRoute compares the path and the query parameters the route names", () => {
    expect(onRoute("/catalog?tab=plots", "/catalog", "?tab=plots&about=x")).toBe(true);
    expect(onRoute("/catalog?tab=plots", "/catalog", "")).toBe(false);
    expect(onRoute("/catalog", "/catalog/", "?tab=models")).toBe(true);
    expect(onRoute("/", "/", "")).toBe(true);
    expect(onRoute("/guided/a/report", "/guided/a/explore", "")).toBe(false);
  });

  it("the make-your-own tour opens the playground and the builder for the user", () => {
    const steps = tourSteps(TOURS.make_your_own, expert);
    const clicks = steps.filter((s) => s.clickOnNext).map((s) => s.anchor);
    expect(clicks).toEqual(["catalog.recipe.try", "library.new-model"]);
    expect(steps.at(-1)?.note).toMatch(/docs/);
  });
});
