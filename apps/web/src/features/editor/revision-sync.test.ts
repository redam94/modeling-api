import { describe, expect, it } from "vitest";

import { shouldLoadRevision } from "./revision-sync";

describe("shouldLoadRevision", () => {
  it("loads the first revision and explicit (re)loads", () => {
    expect(shouldLoadRevision("spc@2", null, false)).toBe(true);
    // An explicit load (Revisions list, banner button) clears the loaded key after the user
    // agreed to discard edits.
    expect(shouldLoadRevision("spc@1", null, true)).toBe(true);
  });

  it("never reloads the revision already in the editor", () => {
    expect(shouldLoadRevision("spc@2", "spc@2", false)).toBe(false);
  });

  it("follows a revision change only while the editor holds no unsaved edits", () => {
    expect(shouldLoadRevision("spc@3", "spc@2", false)).toBe(true);
    // A refetch that moves the selected revision must not throw away unsaved text.
    expect(shouldLoadRevision("spc@3", "spc@2", true)).toBe(false);
  });
});
