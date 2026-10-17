import { describe, expect, it } from "vitest";

import { isGoodMatch } from "./labels";

describe("isGoodMatch", () => {
  it("never calls a 0% or a half match the best match", () => {
    expect(isGoodMatch({ score: 0, reasons_plain: [] })).toBe(false);
    expect(isGoodMatch({ score: 0.5, reasons_plain: [] })).toBe(false);
    expect(isGoodMatch({ score: 0.9, reasons_plain: [] })).toBe(false);
    expect(isGoodMatch({ score: 0.75, reasons_plain: ["Suits: forecast the future"] })).toBe(true);
  });
});
