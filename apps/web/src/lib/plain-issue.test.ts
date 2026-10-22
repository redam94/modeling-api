import { describe, expect, it } from "vitest";

import { plainIssueMessage } from "./plain-issue";

describe("plainIssueMessage (v0.18 live: builder fields showed the validator's own text)", () => {
  it.each([
    ["String should match pattern '^[A-Za-z_][A-Za-z0-9_.\\-]{0,127}$'", "Use letters, digits, _, . and -, and start with a letter or _."],
    ["String should match pattern '^x+$'", "Some of these characters are not allowed here."],
    ["String should have at most 120 characters", "Keep it to 120 characters or fewer."],
    ["String should have at least 1 character", "Fill this in."],
    ["Field required", "Fill this in."],
    ["Extra inputs are not permitted", "This setting is not used here. Remove it."],
    ["Input should be a valid number, unable to parse string as a number", "Type a number here."],
    ["Input should be greater than 0", "Use a number above 0."],
    ["Input should be less than or equal to 1", "Use 1 or less."],
    ["Input should be 'auto', 'log' or 'none'", "Choose one of: auto, log, none."],
    ["List should have at most 8 items after validation, not 9", "Pick at most 8."],
    ["Value error, String should have at most 3 characters", "Keep it to 3 characters or fewer."],
    ["'obs' is not of type 'array'", "This needs a list. Pick one or more."],
    ["'dims' is a required property", "Fill in dims."],
    ["'Ab c' does not match '^[a-z][a-z0-9_]{0,63}$'", "Use lower-case letters, digits and _, and start with a letter."],
    ["'q' is not one of ['a', 'b']", "Choose one of: a, b."],
    ["-2 is less than the minimum of 0", "Use 0 or more."],
    ["Additional properties are not allowed ('colour' was unexpected)", "colour is not a setting here. Remove it."],
  ])("%s", (raw, plain) => {
    expect(plainIssueMessage(raw)).toBe(plain);
  });

  it("leaves the platform's own words alone", () => {
    expect(plainIssueMessage("The scale must be more than 0")).toBe("The scale must be more than 0");
    expect(plainIssueMessage("Variable 'spend' is not on the (week, region) grid of the outcome")).toBe("Variable 'spend' is not on the (week, region) grid of the outcome");
  });
});
