import { afterEach, describe, expect, it } from "vitest";

import { applyNoReferrer, sharePathOf, stripFragment, tokenFromHash } from "./share-paths";

const TOKEN = `mpd_${"k7m2q9x4".repeat(5)}`;

describe("sharePathOf", () => {
  it("matches exactly /s (a trailing slash allowed)", () => {
    expect(sharePathOf("/s")).toBe("/s");
    expect(sharePathOf("/s/")).toBe("/s");
  });

  it("rejects every other path", () => {
    for (const p of ["/", "/sx", "/s/extra", "/share", "/c", "/S", "//s/x", "/signup"]) expect(sharePathOf(p)).toBeNull();
  });
});

describe("tokenFromHash", () => {
  it("returns a well-formed token with or without the leading #", () => {
    expect(tokenFromHash(`#${TOKEN}`)).toBe(TOKEN);
    expect(tokenFromHash(TOKEN)).toBe(TOKEN);
  });

  it("is strict: one Crockford base32 token and nothing else", () => {
    const bad = [
      "",
      "#",
      `#${TOKEN}x`,
      `#${TOKEN.slice(0, -1)}`,
      `#${TOKEN.toUpperCase()}`,
      `#mpd_${"i".repeat(40)}`, // i, l, o and u are not Crockford base32
      `#mpd_${"u".repeat(40)}`,
      `# ${TOKEN}`,
      `#token=${TOKEN}`,
      `#${TOKEN}&passcode=x`,
      `#${TOKEN}#${TOKEN}`,
      `#mev_${"k7m2q9x4".repeat(5)}`,
      `#${encodeURIComponent(TOKEN)}%0A`,
    ];
    for (const h of bad) expect(tokenFromHash(h), h).toBeNull();
  });
});

describe("stripFragment", () => {
  afterEach(() => window.history.replaceState(null, "", "/"));

  it("drops the fragment and keeps the path and query", () => {
    window.history.replaceState(null, "", `/s?x=1#${TOKEN}`);
    const before = window.history.length;
    stripFragment();
    expect(window.location.hash).toBe("");
    expect(window.location.pathname).toBe("/s");
    expect(window.location.search).toBe("?x=1");
    expect(window.location.href).not.toContain(TOKEN);
    // Replaced, not pushed: the back button does not bring the token back.
    expect(window.history.length).toBe(before);
  });

  it("does nothing without a fragment", () => {
    window.history.replaceState(null, "", "/s");
    stripFragment();
    expect(window.location.href).toMatch(/\/s$/);
  });
});

describe("applyNoReferrer", () => {
  it("sets one referrer meta tag to no-referrer", () => {
    applyNoReferrer();
    applyNoReferrer();
    const metas = document.head.querySelectorAll('meta[name="referrer"]');
    expect(metas).toHaveLength(1);
    expect(metas[0]?.getAttribute("content")).toBe("no-referrer");
  });
});
