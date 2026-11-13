import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { PlotFrame } from "./plot-frame";

describe("PlotFrame loading skeleton", () => {
  it("holds the plot's shape with static chart parts while the sandbox draws", () => {
    const { container } = render(<PlotFrame kind="vega-lite" spec={{}} datasets={{}} title="Sales by week" height={240} />);
    const figure = container.querySelector("figure")!;
    expect(figure).toHaveAttribute("aria-busy", "true");
    const skeleton = container.querySelector("[data-slot=plot-skeleton]")!;
    expect(skeleton).toHaveAttribute("aria-hidden", "true");
    expect(skeleton.querySelectorAll(".rounded-t-md").length).toBeGreaterThan(4);
    // Nothing pulses: the spinner is the only animated part (the reduced-motion override stops it).
    const animated = Array.from(skeleton.querySelectorAll("*")).filter((el) => Array.from(el.classList).some((c) => c.startsWith("animate-")));
    expect(animated.map((el) => el.getAttribute("class"))).toEqual([expect.stringContaining("animate-spin")]);
    // e2e screenshot specs wait for `figure .animate-spin` to go: the spinner stays inside the figure.
    expect(figure.querySelector(".animate-spin")).not.toBeNull();
  });
});
