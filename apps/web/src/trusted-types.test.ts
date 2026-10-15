import { describe, expect, it } from "vitest";

import { buildAppCsp } from "../csp";

import { sameOriginScriptUrl } from "./trusted-types";

describe("Trusted Types default policy", () => {
  it("admits same-origin script URLs only", () => {
    expect(sameOriginScriptUrl("/assets/editor.worker.js", "http://127.0.0.1:5173")).toBe("http://127.0.0.1:5173/assets/editor.worker.js");
    expect(() => sameOriginScriptUrl("https://cdn.evil.test/x.js", "http://127.0.0.1:5173")).toThrow(/blocked/);
    expect(() => sameOriginScriptUrl("http://127.0.0.1:5174/x.js", "http://127.0.0.1:5173")).toThrow(/blocked/);
  });
});

describe("app CSP", () => {
  const csp = buildAppCsp({
    apiBase: "http://127.0.0.1:8000/",
    plotsOrigin: "http://127.0.0.1:5174",
    storageOrigins: ["http://127.0.0.1:4443", "https://storage.googleapis.com"],
  });

  it("locks scripts to self and enforces Trusted Types", () => {
    expect(csp).toContain("script-src 'self';");
    expect(csp).not.toMatch(/script-src[^;]*(unsafe|data:|blob:)/);
    expect(csp).toContain("require-trusted-types-for 'script'");
    expect(csp).toContain("object-src 'none'");
  });

  it("connects only to the API and storage origins and frames only the sandbox", () => {
    expect(csp).toContain("connect-src 'self' http://127.0.0.1:8000 http://127.0.0.1:4443 https://storage.googleapis.com");
    expect(csp).toContain("frame-src http://127.0.0.1:5174");
    expect(csp).not.toContain("frame-ancestors");
    expect(buildAppCsp({ apiBase: "http://a.test", plotsOrigin: "http://b.test", storageOrigins: [], header: true })).toContain(
      "frame-ancestors 'none'",
    );
  });
});
