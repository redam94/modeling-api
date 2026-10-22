import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { CatalogPlayground } from "./catalog-playground";

describe("CatalogPlayground", () => {
  it("holds the card's shape while the playground chunk loads, then shows the mini playground", async () => {
    const qc = new QueryClient();
    const { container } = render(
      <QueryClientProvider client={qc}>
        <CatalogPlayground className="mt-2" />
      </QueryClientProvider>,
    );
    const skeleton = container.querySelector("[data-slot=playground-skeleton]");
    expect(skeleton).toHaveAttribute("aria-hidden", "true");
    const card = await screen.findByTestId("playground");
    expect(card).toHaveAttribute("data-variant", "catalog");
    expect(card).toHaveAttribute("data-tour", "catalog.playground");
    expect(card).toHaveClass("mt-2");
    expect(screen.getByRole("heading", { name: "Try the curve" })).toBeInTheDocument();
    expect(container.querySelector("[data-slot=playground-skeleton]")).toBeNull();
  });

  it("keeps the playground and its demo grid out of this module (one lazy import, no static one)", () => {
    const src = readFileSync(resolve(__dirname, "catalog-playground.tsx"), "utf8");
    expect(src).toMatch(/lazy\(\(\) => import\("\.\/playground"\)/);
    expect(src).not.toMatch(/^import .*(?:\.\/playground|demo-grid)/m);
  });
});
