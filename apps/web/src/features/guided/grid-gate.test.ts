import { describe, expect, it } from "vitest";

import { hasNoGrid } from "./api";
import { RESPONSE_GRID_VIEW } from "./whatif";

describe("hasNoGrid", () => {
  it("skips the grid link of a version whose objects hold no grid (R7: a 404 in the builder console)", () => {
    expect(hasNoGrid({ artifacts: [{ name: "manifest.json", size: 1 }, { name: "ui/views/forecast.json", size: 2 }] })).toBe(true);
    expect(hasNoGrid({ artifacts: [{ name: RESPONSE_GRID_VIEW, size: 3 }] })).toBe(false);
    // an empty list says nothing: ask
    expect(hasNoGrid({ artifacts: [] })).toBe(false);
  });
});
