/** The brand mark and the raster art frame. */
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { BrandArt } from "./brand-art";
import { Logo } from "./logo";

describe("Logo", () => {
  it("is three bars: data blue, ink (the text colour), data orange; hidden from assistive tech", () => {
    const { container } = render(<Logo className="size-8" />);
    const svg = container.querySelector("svg")!;
    expect(svg).toHaveAttribute("aria-hidden", "true");
    expect(svg.getAttribute("class")).toContain("size-8");
    expect([...svg.querySelectorAll("rect")].map((r) => r.getAttribute("class"))).toEqual(["fill-series-1", "fill-current", "fill-series-2"]);
    // token classes only: no literal colour in the markup
    for (const r of svg.querySelectorAll("rect")) expect(r.hasAttribute("fill") || r.hasAttribute("style")).toBe(false);
  });

  it("has a default size", () => {
    const { container } = render(<Logo />);
    expect(container.querySelector("svg")!.getAttribute("class")).toContain("size-6");
  });
});

describe("BrandArt", () => {
  it("is a decorative image with its pixel size, loaded lazily and decoded off the main thread", () => {
    const { container } = render(<BrandArt src="/assets/empty-runs.webp" width={480} height={480} className="size-28" />);
    const img = container.querySelector("img")!;
    expect(img).toHaveAttribute("src", "/assets/empty-runs.webp");
    expect(img).toHaveAttribute("width", "480");
    expect(img).toHaveAttribute("height", "480");
    expect(img).toHaveAttribute("loading", "lazy");
    expect(img).toHaveAttribute("decoding", "async");
    expect(img).toHaveAttribute("alt", "");
    expect(img).toHaveAttribute("aria-hidden", "true");
    expect(img).toHaveAttribute("data-slot", "brand-art");
    // drawn on black: the blend utility serves both themes
    expect(img.className).toContain("art-on-black");
    expect(img.className).toContain("size-28");
  });

  it("can keep its own colours on a band that is dark in both themes", () => {
    const { container } = render(<BrandArt src="/assets/signin.webp" width={1200} height={1500} blend={false} />);
    expect(container.querySelector("img")!.className).not.toMatch(/art-on-black|mix-blend/);
  });

  it("drops the black on a block that is dark in both themes", () => {
    const { container } = render(<BrandArt src="/assets/empty-models.webp" width={480} height={480} blend="screen" />);
    const cls = container.querySelector("img")!.className;
    expect(cls).toContain("mix-blend-screen");
    expect(cls).not.toContain("art-on-black");
  });
});
