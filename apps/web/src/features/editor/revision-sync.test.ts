import { describe, expect, it } from "vitest";

import { saveParent, shouldLoadRevision } from "./revision-sync";

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

describe("saveParent", () => {
  it("sends the revision the text is based on, so the server can detect a concurrent save", () => {
    expect(saveParent(3, 3)).toEqual({ parent: 3, overwrites: null });
    // Cached latest is stale (older than what the editor holds): still the base revision.
    expect(saveParent(4, 3)).toEqual({ parent: 4, overwrites: null });
    expect(saveParent(2, undefined)).toEqual({ parent: 2, overwrites: null });
  });

  it("asks before saving text based on an older revision over a newer latest", () => {
    expect(saveParent(1, 3)).toEqual({ parent: 3, overwrites: 3 });
  });
});
