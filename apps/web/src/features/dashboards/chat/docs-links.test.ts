import { describe, expect, it } from "vitest";

import { normaliseDocsBase } from "@/env";

import { docsHref } from "./docs-links";

describe("docs links (web+docs-2)", () => {
  it("reads VITE_DOCS_URL as an http(s) site root with a trailing slash", () => {
    expect(normaliseDocsBase(undefined)).toBeNull();
    expect(normaliseDocsBase("  ")).toBeNull();
    expect(normaliseDocsBase("javascript:alert(1)")).toBeNull();
    expect(normaliseDocsBase("not a url")).toBeNull();
    expect(normaliseDocsBase("https://docs.example.com")).toBe("https://docs.example.com/");
    expect(normaliseDocsBase("https://example.com/mapi/")).toBe("https://example.com/mapi/");
  });

  it("links a notice to its guide section when a docs site is set, and to nothing otherwise", () => {
    const base = normaliseDocsBase("https://example.com/mapi");
    expect(docsHref("unverified_numbers", base)).toBe("https://example.com/mapi/guides/ask-about-your-dashboard/#check-the-numbers");
    expect(docsHref("budget", base)).toBe("https://example.com/mapi/guides/ask-about-your-dashboard/#daily-limits");
    expect(docsHref("unverified_numbers", null)).toBeNull();
  });
});
