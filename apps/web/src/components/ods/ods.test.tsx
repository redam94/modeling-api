import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { CountUp } from "./count-up";
import { acceptsFile, FileDrop } from "./file-drop";
import { dm, dmy, fixed, grouped, MINUS, signed } from "./format";
import { InlineMessage } from "./inline-message";
import { Legend, ZoneLegend } from "./legend";
import { ProgressRing } from "./progress-ring";
import { StatTile } from "./stat-tile";
import { SummaryStrip } from "./summary-strip";
import { ZoneTag } from "./tag-word";

describe("format", () => {
  it("uses the U+2212 minus and DD/MM/YYYY dates", () => {
    expect(fixed(-0.123, 2)).toBe(`${MINUS}0.12`);
    expect(fixed(-0.001, 2)).toBe("0.00");
    expect(signed(0.04)).toBe("+0.04");
    expect(signed(-1.5, 1)).toBe(`${MINUS}1.5`);
    expect(grouped(-12400)).toBe(`${MINUS}12,400`);
    expect(fixed(null)).toBe("—");
    expect(dmy("2026-10-18")).toBe("18/10/2026");
    expect(dm("2026-10-18T00:00:00Z")).toBe("18/10");
  });
});

describe("StatTile", () => {
  it("says the delta direction in words, not only in colour", () => {
    const { container } = render(<StatTile label="Expected result" value="12,400" delta="4.2 % against the plan" deltaDir="up" caption="Week to 18/10/2026" />);
    const delta = container.querySelector("[data-slot=delta]");
    expect(delta).toHaveAttribute("data-dir", "up");
    expect(delta).toHaveClass("text-success");
    expect(delta?.textContent).toBe("Up 4.2 % against the plan");
    expect(screen.getByText("Week to 18/10/2026")).toBeInTheDocument();
  });

  it("uses Down for a fall and no direction word for flat or alert", () => {
    const { container, rerender } = render(<StatTile label="Return on media" value="1.84" delta="0.12" deltaDir="down" />);
    expect(container.querySelector("[data-slot=delta]")?.textContent).toBe("Down 0.12");
    rerender(<StatTile label="Return on media" value="1.84" delta="No change" deltaDir="flat" />);
    expect(container.querySelector("[data-slot=delta]")?.textContent).toBe("No change");
    expect(container.querySelector("[data-slot=delta] svg")).toBeNull();
    rerender(<StatTile label="Accuracy" value="—" />);
    expect(container.querySelector("[data-slot=delta]")).toBeNull();
  });
});

describe("InlineMessage", () => {
  it("says its tone in words and picks a role from it", () => {
    const { rerender } = render(<InlineMessage tone="error">The file has no KPI column.</InlineMessage>);
    const alert = screen.getByRole("alert");
    expect(alert).toHaveAttribute("data-tone", "error");
    expect(alert.textContent).toBe("Error: The file has no KPI column.");
    rerender(<InlineMessage tone="warning">Live inputs are paused.</InlineMessage>);
    expect(screen.getByRole("status")).toHaveTextContent("Warning: Live inputs are paused.");
    rerender(
      <InlineMessage tone="success" role="none">
        Saved.
      </InlineMessage>,
    );
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.getByText("Saved.").closest("[data-slot=inline-message]")).toHaveAttribute("data-tone", "success");
  });
});

describe("FileDrop", () => {
  it("opens the file picker from the keyboard", async () => {
    const user = userEvent.setup();
    const click = vi.spyOn(HTMLInputElement.prototype, "click").mockImplementation(() => undefined);
    render(<FileDrop onFiles={() => undefined} accept=".csv" hint="A .csv file with one row for each week." />);
    const zone = screen.getByRole("button", { name: "Drop a file here, or choose a file" });
    expect(zone).toHaveAccessibleDescription("A .csv file with one row for each week.");
    await user.tab();
    expect(zone).toHaveFocus();
    await user.keyboard("{Enter}");
    await user.keyboard(" ");
    expect(click).toHaveBeenCalledTimes(2);
    click.mockRestore();
  });

  it("passes accepted files on and names the files it leaves out", async () => {
    const onFiles = vi.fn();
    const { container } = render(<FileDrop onFiles={onFiles} accept=".csv" />);
    const input = container.querySelector("input[type=file]") as HTMLInputElement;
    const csv = new File(["week,sales\n"], "actuals.csv", { type: "text/csv" });
    await userEvent.upload(input, csv);
    expect(onFiles).toHaveBeenCalledWith([csv]);

    const pdf = new File(["%PDF"], "report.pdf", { type: "application/pdf" });
    fireEvent.drop(screen.getByRole("button"), { dataTransfer: { files: [pdf] } });
    expect(onFiles).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("alert")).toHaveTextContent("You cannot use the file report.pdf. Use a file of this type: .csv.");
  });

  it("matches accept rules by extension, exact type and wildcard", () => {
    expect(acceptsFile({ name: "A.CSV", type: "" }, ".csv")).toBe(true);
    expect(acceptsFile({ name: "a.txt", type: "text/csv" }, "text/csv")).toBe(true);
    expect(acceptsFile({ name: "a.png", type: "image/png" }, "image/*")).toBe(true);
    expect(acceptsFile({ name: "a.png", type: "image/png" }, ".csv")).toBe(false);
    expect(acceptsFile({ name: "a.png", type: "image/png" }, undefined)).toBe(true);
  });
});

describe("tags, legends and strips", () => {
  it("pairs every zone colour with its tag word", () => {
    const { rerender } = render(<ZoneTag zone="over" />);
    expect(screen.getByText("Past its best")).toHaveAttribute("data-tone", "negative");
    rerender(<ZoneTag zone={null} />);
    expect(screen.getByText("No zone")).toBeInTheDocument();
    render(<ZoneLegend />);
    const key = screen.getByRole("list", { name: "Zones" });
    expect(key.textContent).toBe("More roomWithin rangePast its bestNot paying back");
  });

  it("renders legend items and summary cells", () => {
    render(<Legend items={[{ label: "Expected", color: "index", swatch: "line" }, { label: "Actual", color: "net", swatch: "dot" }]} />);
    expect(screen.getAllByRole("listitem").map((li) => li.textContent)).toEqual(["Expected", "Actual"]);
    render(<SummaryStrip label="This week" items={[{ key: "a", label: "Spend", value: "£42,000", sub: "4 channels" }, { key: "b", label: "Result", value: "12,400" }]} />);
    const strip = screen.getByLabelText("This week");
    expect(strip.querySelectorAll("dt")).toHaveLength(2);
    expect(strip).toHaveTextContent("£42,000");
  });
});

describe("axisDate", () => {
  it("keeps DD/MM while the labels differ, and adds the year when they would repeat", async () => {
    const { axisDate } = await import("./format");
    expect(axisDate(["2026-01-05", "2026-01-12"])("2026-01-12")).toBe("12/01");
    const quarters = ["2011-10-01", "2012-10-01", "2013-10-01", "2014-10-01"];
    expect(quarters.map(axisDate(quarters))).toEqual(["10/2011", "10/2012", "10/2013", "10/2014"]);
    const days = ["2011-03-05", "2012-03-05"];
    expect(days.map(axisDate(days))).toEqual(["05/03/11", "05/03/12"]);
    expect(axisDate(["case 1", "case 2"])("case 2")).toBe("case 2");
  });
});

describe("ProgressRing", () => {
  it("is a progressbar that says how far along it is", () => {
    const { rerender } = render(<ProgressRing value={2} max={5} label="Getting started" />);
    const ring = screen.getByRole("progressbar", { name: "Getting started" });
    expect(ring).toHaveAttribute("aria-valuenow", "2");
    expect(ring).toHaveAttribute("aria-valuemax", "5");
    expect(ring).toHaveAttribute("aria-valuetext", "2 of 5");
    expect(ring.querySelector("text")?.textContent).toBe("2/5");
    rerender(<ProgressRing value={9} max={5} label="Getting started" showText={false} />);
    expect(ring).toHaveAttribute("aria-valuenow", "5");
    expect(ring.querySelector("text")).toBeNull();
    expect(ring.querySelectorAll("circle")).toHaveLength(2);
    rerender(<ProgressRing value={0} max={5} label="Getting started" />);
    // No round-capped dot for nothing done: only the track.
    expect(ring.querySelectorAll("circle")).toHaveLength(1);
  });
});

describe("CountUp", () => {
  function reducedMotion(on: boolean) {
    const original = window.matchMedia;
    window.matchMedia = (query: string) => ({ ...original(query), matches: on && query.includes("reduce") });
    return () => {
      window.matchMedia = original;
    };
  }

  it("always has the final text in the DOM, also while the number moves", () => {
    vi.useFakeTimers({ toFake: ["requestAnimationFrame", "cancelAnimationFrame", "performance"] });
    try {
      const format = (v: number) => grouped(Math.round(v));
      const { container, rerender } = render(<CountUp value={1000} format={format} />);
      const root = container.querySelector("[data-slot=count-up]")!;
      expect(root.textContent).toBe("1,000");
      rerender(<CountUp value={12400} format={format} />);
      act(() => {
        vi.advanceTimersByTime(100);
      });
      // Final text present and readable; the moving copy is hidden from assistive tech.
      expect(screen.getByText("12,400")).toBeInTheDocument();
      const moving = root.querySelector("[aria-hidden]");
      expect(moving).not.toBeNull();
      expect(moving?.textContent).not.toBe("12,400");
      act(() => {
        vi.advanceTimersByTime(1000);
      });
      expect(root.textContent).toBe("12,400");
      expect(root.querySelector("[aria-hidden]")).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("jumps straight to the end value under reduced motion", () => {
    const restore = reducedMotion(true);
    try {
      const { container } = render(<CountUp value={42} from={0} />);
      const root = container.querySelector("[data-slot=count-up]")!;
      expect(root.textContent).toBe("42");
      expect(root.querySelector("[aria-hidden]")).toBeNull();
    } finally {
      restore();
    }
  });
});
