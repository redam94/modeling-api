import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { MINUS } from "./ods/format";
import { BandLine, DivergingBars, IndexLine, indexDomain, niceDomain, RangeBar, Sparkline, ZoneBar } from "./readout-charts";

const WEEKS = ["2026-09-27", "2026-10-04", "2026-10-11", "2026-10-18"];

describe("indexDomain", () => {
  it("is symmetric about the reference, at least ±0.10, with at most three ticks a side", () => {
    const d = indexDomain([0.95, 1.03]);
    expect([d.lo, d.hi]).toEqual([0.9, 1.1]);
    expect(d.ticks).toEqual([0.9, 0.95, 1, 1.05, 1.1]);

    const wide = indexDomain([0.6, 1.1]);
    expect([wide.lo, wide.hi]).toEqual([0.6, 1.4]);
    expect(wide.ticks).toEqual([0.6, 0.8, 1, 1.2, 1.4]);

    const pct = indexDomain([88, 131], { centre: 100, min: 10 });
    expect(pct.hi - 100).toBeCloseTo(100 - pct.lo);
    expect(pct.ticks.filter((t) => t > 100).length).toBeLessThanOrEqual(3);
    expect(pct.ticks).toContain(100);
  });

  it("widens a flat series to round ticks", () => {
    expect(niceDomain([5, 5]).ticks.length).toBeGreaterThan(1);
    expect(niceDomain([0, 1000])).toMatchObject({ lo: 0, hi: 1000 });
  });
});

describe("IndexLine", () => {
  const points = [
    { t: WEEKS[0] as string, value: 1.02, low: 0.98, high: 1.06 },
    { t: WEEKS[1] as string, value: 1.04, low: 1.0, high: 1.08 },
    { t: WEEKS[2] as string, value: 0.97, low: 0.93, high: 1.01 },
    { t: WEEKS[3] as string, value: 0.99, low: 0.95, high: 1.03 },
  ];

  it("names the numbers: end value, range, extremes and model changes", () => {
    const { container } = render(<IndexLine label="Media effectiveness index" points={points} versions={[{ at: 2, label: "Model v4" }]} />);
    const chart = screen.getByRole("img");
    const name = chart.getAttribute("aria-label") ?? "";
    expect(name).toContain("Media effectiveness index, 4 weeks to 18/10/2026: 0.99 this week, likely range 0.95 to 1.03.");
    expect(name).toContain("The reference is 1.00.");
    expect(name).toContain("Lowest 0.97 on 11/10/2026, highest 1.04 on 04/10/2026.");
    expect(name).toContain("Model v4 from 11/10/2026.");
    expect(container.querySelector("[data-end-label]")?.textContent).toBe("0.99 this week");
    expect(container.querySelectorAll("[data-marker=version]")).toHaveLength(1);
    expect(container.querySelector("[data-marker=version]")).toHaveAttribute("data-at", "2");
    // The hidden table carries every value and the change note.
    const rows = container.querySelectorAll("table tbody tr");
    expect(rows).toHaveLength(4);
    expect(rows[2]?.textContent).toBe("11/10/20260.970.93 to 1.01Model v4");
  });

  it("draws at the container width with a symmetric axis about the reference", () => {
    const { container } = render(<IndexLine label="Index" points={points} nowAt={3} />);
    const svg = container.querySelector("svg");
    expect(svg).toHaveAttribute("height", "240");
    expect(svg?.getAttribute("viewBox")).toBe(`0 0 ${svg?.getAttribute("width")} 240`);
    const ticks = [...container.querySelectorAll("text")].map((t) => t.textContent).filter((t) => /^\d\.\d\d$/.test(t ?? ""));
    expect(ticks).toEqual(["0.90", "0.95", "1.00", "1.05", "1.10"]);
    expect(container.querySelector("[data-marker=now]")).not.toBeNull();
  });

  it("puts a falling reading's label below the dot and says when there are no values", () => {
    const { rerender } = render(<IndexLine label="Index" points={[]} />);
    expect(screen.getByRole("img").getAttribute("aria-label")).toContain("There are no values.");
    rerender(<IndexLine label="Index" period="month" points={[{ t: "2026-10-31", value: 0.92 }]} />);
    expect(screen.getByRole("img").getAttribute("aria-label")).toContain("1 month to 31/10/2026: 0.92 this month.");
  });
});

describe("BandLine", () => {
  it("gives the latest actual against the expectation and the band hit rate", () => {
    const points = [
      { t: WEEKS[0] as string, mean: 1000, low: 900, high: 1100, actual: 1050 },
      { t: WEEKS[1] as string, mean: 1000, low: 900, high: 1100, actual: 1200 },
      { t: WEEKS[2] as string, mean: 1100, low: 1000, high: 1200, actual: 1150 },
      { t: WEEKS[3] as string, mean: 1100, low: 1000, high: 1200, actual: null },
    ];
    render(<BandLine label="Sales, expected and actual" points={points} />);
    const name = screen.getByRole("img").getAttribute("aria-label") ?? "";
    expect(name).toContain("Latest actual 1,150 on 11/10/2026, against expected 1,100 (likely range 1,000 to 1,200).");
    expect(name).toContain("in the likely range in 2 of 3 weeks.");
    expect(screen.getByRole("list", { name: "Key" }).textContent).toBe("ExpectedLikely rangeActual");
  });
});

describe("DivergingBars", () => {
  it("lists signed values with the U+2212 minus and draws gains and losses apart", () => {
    const { container } = render(
      <DivergingBars
        label="Why the result differs from the plan"
        rows={[
          { key: "tv", label: "TV", value: 800 },
          { key: "search", label: "Search", value: -1200 },
        ]}
        net={{ label: "Net difference", value: -400 }}
      />,
    );
    expect(screen.getByRole("img")).toHaveAccessibleName(
      `Why the result differs from the plan: TV +800, Search ${MINUS}1,200; Net difference ${MINUS}400.`,
    );
    expect(container.querySelectorAll("[data-sign=gain]")).toHaveLength(1);
    expect(container.querySelectorAll("[data-sign=loss]")).toHaveLength(2);
    expect(container.querySelector("[data-row=net] [data-sign]")).toHaveClass("bg-chart-net");
    // the longest bar fills its half
    expect((container.querySelector("[data-row=item]:nth-child(2) [data-sign]") as HTMLElement).style.width).toBe("50%");
  });
});

describe("meters", () => {
  it("ZoneBar names the zone in words and the spend as a multiple", () => {
    const { container } = render(<ZoneBar label="TV" zone="over" now={1.4} low={0.6} high={1.1} />);
    expect(screen.getByRole("img")).toHaveAccessibleName(
      "TV: Past its best. Spend this week is 1.40 times the usual level. The best range is 0.60 to 1.10 times.",
    );
    expect(container.querySelector("[data-now]")).toHaveStyle({ left: `${(1.4 / 2.4) * 100}%` });
  });

  it("ZoneBar draws one grey track when no spend pays back", () => {
    const { container } = render(<ZoneBar label="Print" zone="no_payback" now={0.8} low={0.6} high={1.1} />);
    expect(screen.getByRole("img")).toHaveAccessibleName("Print: Not paying back. Spend this week is 0.80 times the usual level.");
    expect(container.querySelector(".bg-zone-optimal")).toBeNull();
  });

  it("RangeBar says where the range sits against the reference", () => {
    const { rerender } = render(<RangeBar label="TV return on media" value={1.8} low={1.2} high={2.4} />);
    const bar = screen.getByRole("img");
    expect(bar).toHaveAccessibleName("TV return on media: 1.80, likely range 1.20 to 2.40, above the break-even of 1.00.");
    expect(bar).toHaveAttribute("data-dir", "up");
    rerender(<RangeBar label="Print return on media" value={0.9} low={0.7} high={1.1} />);
    expect(screen.getByRole("img")).toHaveAccessibleName("Print return on media: 0.90, likely range 0.70 to 1.10, around the break-even of 1.00.");
  });

  it("Sparkline gives first, last and extremes with a dashed reference", () => {
    const { container } = render(<Sparkline label="Search reading" values={[0.98, 1.06, null, 1.04]} reference={1} />);
    expect(screen.getByRole("img")).toHaveAccessibleName("Search reading, 4 values: from 0.98 to 1.04, lowest 0.98, highest 1.06. The reference is 1.00.");
    expect(container.querySelector("svg")).toHaveAttribute("height", "48");
    expect(container.querySelector("[data-ref]")).toHaveAttribute("stroke-dasharray", "3 3");
  });
});
