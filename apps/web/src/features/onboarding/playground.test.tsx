import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { onOnboarding, type OnboardingMessage } from "@/lib/onboarding-events";

import {
  channelCurves,
  channelLabel,
  deltaText,
  deltaTone,
  demoGrid,
  MiniPlayground,
  money,
  nextPound,
  niceTicks,
  orderedChannels,
  paysBackRanges,
  Playground,
  PLAYGROUND_STEP,
  playgroundView,
  planMultipliers,
  smoothPath,
  spendWords,
} from "./playground";

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("demo grid", () => {
  it("parses as a response grid with the three sample channels", () => {
    const g = demoGrid();
    expect(g.format).toBe("mapi.response-grid/v1");
    expect(orderedChannels(g)).toEqual(["tv", "search", "social"]);
    expect(g.multipliers[0]).toBe(0);
    expect(g.multipliers.includes(1)).toBe(true);
    expect(g.baseline).not.toBeNull();
  });

  it("is finite over the whole slider range, for every channel alone and all together", () => {
    const g = demoGrid();
    const curves = channelCurves(g);
    const max = g.multipliers[g.multipliers.length - 1] as number;
    const finite = (v: number | null) => v === null || Number.isFinite(v);
    for (let i = 0; i * PLAYGROUND_STEP <= max + 1e-9; i++) {
      const m = Math.min(max, Math.round(i * PLAYGROUND_STEP * 100) / 100);
      const scenarios = [...g.channels.map((ch) => ({ ...planMultipliers(g), [ch]: m })), Object.fromEntries(g.channels.map((ch) => [ch, m]))];
      for (const mult of scenarios) {
        const v = playgroundView(g, curves, mult);
        expect([v.weeklySales, v.change.lo, v.change.mid, v.change.hi, v.weeklySpend, v.spendChange, v.returnOnChange].every(finite)).toBe(true);
        expect(v.change.lo).toBeLessThanOrEqual(v.change.hi);
        for (const c of v.channels) expect([c.spend, c.added, c.nextPound].every(Number.isFinite)).toBe(true);
      }
    }
    for (const c of curves) expect([...c.spend, ...c.mean, ...c.lo, ...c.hi].every(Number.isFinite)).toBe(true);
  });

  it("answers per week: the plan has no change and spending more adds sales", () => {
    const g = demoGrid();
    const curves = channelCurves(g);
    const plan = playgroundView(g, curves, planMultipliers(g));
    expect(plan.change.mid).toBeCloseTo(0, 6);
    expect(plan.spendChange).toBeCloseTo(0, 6);
    expect(plan.returnOnChange).toBeNull();
    // Weekly numbers: the grid holds totals over 96 weeks.
    expect(plan.weeklySales).toBeGreaterThan(100_000);
    expect(plan.weeklySales).toBeLessThan(5_000_000);
    const more = playgroundView(g, curves, { ...planMultipliers(g), tv: 1.5 });
    expect(more.change.mid).toBeGreaterThan(0);
    expect(more.spendChange).toBeGreaterThan(0);
    // TV is concave (WP0 probe (j)): the next £1 returns less at a higher spend.
    const tv = curves[g.channels.indexOf("tv")]!;
    expect(nextPound(g, tv, 2)).toBeLessThan(nextPound(g, tv, 0.5));
  });

  it("finds where the next £1 pays back (concave TV from zero; S-shaped search in a band)", () => {
    const g = demoGrid();
    const curves = channelCurves(g);
    const tv = paysBackRanges(g, curves[g.channels.indexOf("tv")]!);
    expect(tv.length).toBeGreaterThan(0);
    expect(tv[0]![0]).toBe(0);
    for (const ranges of [tv, paysBackRanges(g, curves[g.channels.indexOf("search")]!)]) {
      for (const [a, b] of ranges) expect(a).toBeLessThanOrEqual(b);
    }
  });
});

describe("words and numbers", () => {
  it("formats money the way analysts say it", () => {
    expect(money(729_400)).toBe("£729k");
    expect(money(1_240_000)).toBe("£1.24M");
    expect(money(2_340)).toBe("£2.3k");
    expect(money(950)).toBe("£950");
    expect(money(-12_000)).toBe("−£12k");
    expect(money(Number.NaN)).toBe("—");
  });

  it("says the change against the plan", () => {
    expect(deltaTone(10)).toBe("flat");
    expect(deltaText(0)).toBe("same as plan");
    expect(deltaTone(12_000)).toBe("up");
    expect(deltaText(12_000)).toBe("+£12k vs plan");
    expect(deltaTone(-3_200)).toBe("down");
    expect(deltaText(-3_200)).toBe("−£3.2k vs plan");
    expect(spendWords(1)).toBe("as planned");
    expect(spendWords(0)).toBe("switched off");
    expect(spendWords(1.2)).toBe("+20% vs plan");
    expect(spendWords(0.75)).toBe("−25% vs plan");
    expect(channelLabel("search")).toBe("Paid search");
    expect(channelLabel("radio_local")).toBe("Radio local");
  });

  it("draws round ticks and a smooth path that never leaves the data range", () => {
    expect(niceTicks(145_000)).toEqual([0, 50_000, 100_000, 150_000]);
    expect(niceTicks(0)).toEqual([0]);
    const d = smoothPath([
      [0, 100],
      [10, 50],
      [20, 45],
      [30, 44],
    ]);
    expect(d.startsWith("M0 100C")).toBe(true);
    const ys = [...d.matchAll(/(-?\d+(?:\.\d+)?) (-?\d+(?:\.\d+)?)/g)].map((m) => Number(m[2]));
    expect(Math.min(...ys)).toBeGreaterThanOrEqual(44);
    expect(Math.max(...ys)).toBeLessThanOrEqual(100);
  });
});

describe("Home playground", () => {
  it("moves the delta with a slider, labels the band, and resets to the plan", () => {
    const events: OnboardingMessage[] = [];
    const stop = onOnboarding((m) => events.push(m));
    renderWithQuery(<Playground />);
    const card = screen.getByTestId("playground");
    expect(card).toHaveAttribute("data-tour", "home.playground");
    expect(within(card).getByRole("heading", { name: "What if you moved the budget?" })).toBeInTheDocument();
    expect(within(card).getByText("Example data")).toBeInTheDocument();
    const delta = screen.getByTestId("playground-delta");
    expect(delta).toHaveTextContent("same as plan");
    expect(delta).toHaveAttribute("data-tone", "flat");
    const reset = screen.getByRole("button", { name: /Reset to plan/ });
    expect(reset).toBeDisabled();

    const tv = screen.getByRole("slider", { name: "Weekly spend on TV" });
    expect(tv).toHaveAttribute("aria-valuetext", expect.stringContaining("as planned"));
    fireEvent.keyDown(tv, { key: "ArrowRight" });
    fireEvent.keyDown(tv, { key: "ArrowRight" });
    expect(delta).toHaveAttribute("data-tone", "up");
    expect(delta.textContent).toMatch(/^\+£[\d.]+k? vs plan$/);
    expect(tv).toHaveAttribute("aria-valuetext", expect.stringContaining("+10% vs plan"));
    // The band of the change is one labelled image with the numbers in words.
    expect(screen.getByRole("img", { name: /^Change in weekly sales: \+£.*90% likely between/ })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /^Response curves for TV, Paid search, Social/ })).toBeInTheDocument();
    // The checklist hears about the first move only.
    expect(events.filter((e) => e.event === "whatif_moved")).toEqual([{ event: "whatif_moved", detail: { source: "home" } }]);

    const social = screen.getByRole("slider", { name: "Weekly spend on Social" });
    fireEvent.keyDown(social, { key: "Home" });
    expect(social).toHaveAttribute("aria-valuetext", expect.stringContaining("switched off"));
    expect(events.filter((e) => e.event === "whatif_moved")).toHaveLength(1);

    expect(reset).toBeEnabled();
    fireEvent.click(reset);
    expect(delta).toHaveTextContent("same as plan");
    expect(tv).toHaveAttribute("aria-valuetext", expect.stringContaining("as planned"));
    expect(reset).toBeDisabled();
    stop();
  });

  it("shows what the next £1 returns per channel and no sample link without a session", () => {
    renderWithQuery(<Playground anchor="guided.explore.sliders" />);
    expect(screen.getByTestId("playground")).toHaveAttribute("data-tour", "guided.explore.sliders");
    const tiles = screen.getByRole("group", { name: "What the next £1 would return" });
    expect(within(tiles).getAllByText(/^£\d+\.\d\d$/)).toHaveLength(3);
    expect(screen.queryByRole("button", { name: /Open the real model/ })).toBeNull();
  });
});

describe("Catalog mini playground", () => {
  it("switches channel, slides along the curve and toggles the range and the pays-back zone", () => {
    const { container } = renderWithQuery(<MiniPlayground />);
    const card = screen.getByTestId("playground");
    expect(card).toHaveAttribute("data-tour", "catalog.playground");
    expect(card).toHaveAttribute("data-variant", "catalog");
    expect(screen.getByRole("button", { name: "TV" })).toHaveAttribute("aria-pressed", "true");
    expect(container.querySelector("[data-slot=band]")).not.toBeNull();
    expect(container.querySelector("[data-slot=pays-back]")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Paid search" }));
    expect(screen.getByRole("button", { name: "Paid search" })).toHaveAttribute("aria-pressed", "true");
    const slider = screen.getByRole("slider", { name: "Weekly spend on Paid search" });
    fireEvent.keyDown(slider, { key: "ArrowRight" });
    expect(screen.getByTestId("playground-delta")).toHaveAttribute("data-tone", "up");
    expect(screen.getByTestId("playground-result")).toHaveTextContent(/90% likely between £.* The next £1 returns £\d+\.\d\d\./);

    fireEvent.click(screen.getByRole("switch", { name: "Show 90% range" }));
    expect(container.querySelector("[data-slot=band]")).toBeNull();
    fireEvent.click(screen.getByRole("switch", { name: "Shade where it pays back" }));
    expect(container.querySelectorAll("[data-slot=pays-back]").length).toBeGreaterThan(0);
    expect(screen.getByRole("img", { name: /with the spend where it pays back shaded/ })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Reset to plan/ }));
    expect(screen.getByTestId("playground-delta")).toHaveTextContent("same as plan");
  });
});
