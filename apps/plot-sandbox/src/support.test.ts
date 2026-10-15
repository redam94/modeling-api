import { describe, expect, it } from "vitest";

import { buildCsp, parseOrigins, sandboxHeaders } from "./csp";
import { toBase64DataUrl, utf8ToBase64 } from "./renderers";
import { TextTooltip, tooltipEntries } from "./tooltip";

describe("csp", () => {
  it("builds a policy with no unsafe script sources", () => {
    const csp = buildCsp({ appOrigins: ["http://127.0.0.1:5173"] });
    expect(csp).toContain("default-src 'none'");
    expect(csp).toContain("script-src 'self'");
    expect(csp).not.toMatch(/script-src[^;]*unsafe/);
    expect(csp).toContain("img-src data: blob:");
    expect(csp).not.toContain("frame-ancestors");
  });

  it("adds frame-ancestors to the header and the HMR socket in dev", () => {
    const headers = sandboxHeaders({
      appOrigins: ["http://127.0.0.1:5173"],
      dev: true,
      selfOrigin: "http://127.0.0.1:5174",
    });
    const csp = headers["Content-Security-Policy"] ?? "";
    expect(csp).toContain("frame-ancestors http://127.0.0.1:5173");
    expect(csp).toContain("connect-src 'self' ws://127.0.0.1:5174");
  });

  it("validates origins", () => {
    expect(parseOrigins(" http://a.test:1/ , https://b.test ", [])).toEqual(["http://a.test:1", "https://b.test"]);
    expect(parseOrigins("", ["http://x.test"])).toEqual(["http://x.test"]);
    expect(() => parseOrigins("javascript:alert(1)", [])).toThrow();
    expect(() => parseOrigins("http://a.test/path", [])).toThrow();
  });
});

describe("data URLs", () => {
  it("encodes UTF-8 to base64", () => {
    expect(atob(utf8ToBase64("é"))).toBe("Ã©");
  });

  it("normalises URL-encoded SVG and passes base64 PNG", () => {
    expect(toBase64DataUrl("data:image/svg+xml,%3Csvg%3E", "svg")).toBe(
      `data:image/svg+xml;base64,${btoa("<svg>")}`,
    );
    expect(toBase64DataUrl("data:image/png;base64,AAAA", "png")).toBe("data:image/png;base64,AAAA");
    expect(() => toBase64DataUrl("blob:abc", "png")).toThrow();
    expect(() => toBase64DataUrl("data:text/html,<b>", "svg")).toThrow();
  });
});

describe("TextTooltip", () => {
  it("renders values as text, never markup", () => {
    expect(tooltipEntries({ a: 1.23456789, b: "<img src=x onerror=alert(1)>" })).toEqual([
      ["a", "1.23457"],
      ["b", "<img src=x onerror=alert(1)>"],
    ]);
    const tip = new TextTooltip(document);
    tip.handler(null, new MouseEvent("mousemove", { clientX: 10, clientY: 10 }), null, {
      name: "<script>alert(1)</script>",
    });
    const el = document.querySelector(".mapi-tooltip");
    expect(el?.querySelector("script")).toBeNull();
    expect(el?.textContent).toContain("<script>alert(1)</script>");
    tip.handler(null, new MouseEvent("mousemove"), null, null);
    expect((el as HTMLElement).hidden).toBe(true);
  });
});
