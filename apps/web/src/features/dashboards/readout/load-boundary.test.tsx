import { render, screen } from "@testing-library/react";
import { lazy } from "react";
import { describe, expect, it, vi } from "vitest";

import { LazyPart, LOAD_FAILED } from "./load-boundary";

function Boom(): never {
  throw new Error("unexpected snapshot data");
}

describe("LazyPart", () => {
  it("shows a plain message in the failed part only, the rest of the readout stays", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const Gone = lazy(() => Promise.reject(new Error("Failed to fetch dynamically imported module")));
    render(
      <div>
        <p>The lead stays.</p>
        <LazyPart label="Try a case">
          <Gone />
        </LazyPart>
        <LazyPart>
          <Boom />
        </LazyPart>
      </div>,
    );
    expect(await screen.findByText(`Try a case: ${LOAD_FAILED}`)).toBeTruthy();
    expect(screen.getAllByRole("alert")).toHaveLength(2);
    expect(screen.getByText("The lead stays.")).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "Reload the page" })).toHaveLength(2);
  });
});
