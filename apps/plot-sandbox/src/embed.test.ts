import { tableFromArrays, tableToIPC } from "apache-arrow";
import { describe, expect, it, vi } from "vitest";

import { decodeArrowBase64 } from "./arrow";
import { API_URL_PLACEHOLDER, buildCsp, buildEmbedCsp, embedHeaders, parseApiUrl } from "./csp";
import {
  decodeDatasets,
  EmbedPage,
  frameAllowed,
  loadEmbed,
  parseEmbedView,
  tokenFromPath,
  type EmbedElements,
} from "./embed";
import type { Renderer } from "./renderers";

const API = "https://api.mapi.example";
const TOKEN = "mpe_0123456789abcdefghjkmnpqrstvwxyzabcdefgh";

function b64(bytes: Uint8Array): string {
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin);
}

const ARROW = b64(tableToIPC(tableFromArrays({ t: Int32Array.from([1, 2, 3]), y: Float64Array.from([1.5, NaN, 3]) })));

function view(overrides: Record<string, unknown> = {}, plot: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    title: "EU response curves",
    caption_plain: "Spend against expected revenue.",
    plot: {
      id: "mmm.response_curves",
      title: "Response curves",
      kind: "vega-lite",
      schema_version: "v5",
      spec: { mark: "line", data: { name: "fan" }, encoding: { x: { field: "t", type: "quantitative" } } },
      datasets: { fan: { kind: "inline_arrow", data: ARROW, rows: 3 } },
      provenance: { recipe: "mmm.response_curves@1.0.0", generated_at: "2026-10-15T09:40:00Z" },
      ...plot,
    },
    generated_at: "2026-10-15T09:40:00Z",
    expires_at: "2027-01-13T09:30:00Z",
    allowed_origins: [],
    image_url: null,
    ...overrides,
  };
}

function response(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
}

function elements(): EmbedElements {
  const png = document.createElement("a");
  png.hidden = true;
  return {
    title: document.createElement("h1"),
    plot: document.createElement("div"),
    caption: document.createElement("p"),
    status: document.createElement("p"),
    png,
    meta: document.createElement("span"),
  };
}

describe("embed route", () => {
  it("reads the token from /embed/<token> only", () => {
    expect(tokenFromPath(`/embed/${TOKEN}`)).toBe(TOKEN);
    expect(tokenFromPath(`/embed/${TOKEN}/`)).toBe(TOKEN);
    expect(tokenFromPath("/embed/mpe_short")).toBeNull();
    expect(tokenFromPath(`/embed/${TOKEN}/x`)).toBeNull();
    expect(tokenFromPath(`/other/${TOKEN}`)).toBeNull();
    expect(tokenFromPath(`/embed/${TOKEN.toUpperCase()}`)).toBeNull();
  });
});

describe("parseEmbedView", () => {
  it("accepts a platform-recipe view with inline data", () => {
    const v = parseEmbedView(view({ allowed_origins: ["https://acme.notion.site"], image_url: "https://storage.example/x.png?sig=1" }));
    expect(v).not.toBeNull();
    expect(v?.plot.datasets.fan).toBe(ARROW);
    expect(v?.allowedOrigins).toEqual(["https://acme.notion.site"]);
    expect(v?.imageUrl).toBe("https://storage.example/x.png?sig=1");
  });

  it.each([
    ["a data_ref dataset", view({}, { datasets: { fan: { kind: "data_ref", path: "ui/x.arrow" } } })],
    ["url data", view({}, { spec: { mark: "line", data: { url: "https://evil.example/d.json" } } })],
    ["an image mark", view({}, { spec: { mark: "image", encoding: { url: { value: "https://evil.example" } } } })],
    ["a prototype key", JSON.parse(`{"title":"x","plot":{"id":"p","kind":"vega-lite","spec":{"__proto__":{}},"datasets":{}},"expires_at":"2027-01-01"}`)],
    ["an unknown kind", view({}, { kind: "d3" })],
    ["a non-https origin", view({ allowed_origins: ["http://acme.example"] })],
    ["a javascript: image link", view({ image_url: "javascript:alert(1)" })],
    ["a non-base64 dataset", view({}, { datasets: { fan: { kind: "inline_arrow", data: "<svg>" } } })],
    ["not an object", "nope"],
  ])("refuses %s", (_what, body) => {
    expect(parseEmbedView(body)).toBeNull();
  });

  it("keeps untrusted text as text", () => {
    const v = parseEmbedView(view({ title: "<img src=x onerror=alert(1)>\u0007" }));
    expect(v?.title).toBe("<img src=x onerror=alert(1)> ");
  });
});

describe("datasets", () => {
  it("decodes inline Arrow into rows of scalars", () => {
    const rows = decodeArrowBase64(ARROW);
    expect(rows).toEqual([
      { t: 1, y: 1.5 },
      { t: 2, y: null },
      { t: 3, y: 3 },
    ]);
  });

  it("enforces the row budget across datasets", () => {
    expect(() => decodeDatasets({ a: ARROW, b: ARROW }), "within budget").not.toThrow();
    expect(() => decodeArrowBase64(ARROW, 2)).toThrow(/at most 2/);
    expect(() => decodeArrowBase64("not-base64!")).toThrow();
  });
});

describe("frameAllowed", () => {
  const allowed = ["https://acme.notion.site"];
  it.each([
    [[], { framed: true, ancestorOrigins: ["https://anywhere.example"], referrer: "" }, true],
    [allowed, { framed: false, ancestorOrigins: null, referrer: "" }, true],
    [allowed, { framed: true, ancestorOrigins: ["https://acme.notion.site"], referrer: "" }, true],
    [allowed, { framed: true, ancestorOrigins: ["https://evil.example"], referrer: "https://acme.notion.site/" }, false],
    [allowed, { framed: true, ancestorOrigins: null, referrer: "https://acme.notion.site/page/1" }, true],
    [allowed, { framed: true, ancestorOrigins: null, referrer: "https://evil.example/" }, false],
    [allowed, { framed: true, ancestorOrigins: null, referrer: "" }, false],
  ])("allowed=%j frame=%j -> %s", (origins, frame, expected) => {
    expect(frameAllowed(origins, frame)).toBe(expected);
  });
});

describe("loadEmbed", () => {
  it("fetches the API's embed path without credentials or referrer", async () => {
    const fetchFn = vi.fn(async () => response(200, view()));
    const res = await loadEmbed(API, TOKEN, fetchFn);
    expect(res.ok).toBe(true);
    const [url, init] = fetchFn.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`${API}/v1/embeds/${TOKEN}`);
    expect(init.credentials).toBe("omit");
    expect(init.referrerPolicy).toBe("no-referrer");
    expect(init.mode).toBe("cors");
  });

  it.each([
    [404, {}, /does not exist/],
    [410, {}, /expired or was revoked/],
    [429, { "Retry-After": "17" }, /Try again in 17 s/],
    [500, {}, /could not be loaded/],
  ])("explains a %i", async (status, headers, message) => {
    const fetchFn = vi.fn(async () => response(status, { code: "X" }, headers));
    const res = await loadEmbed(API, TOKEN, fetchFn);
    expect(res.ok).toBe(false);
    if (!res.ok) expect(res.message).toMatch(message);
  });

  it("refuses an unsafe answer and survives a network error", async () => {
    const unsafe = vi.fn(async () => response(200, view({}, { spec: { mark: "image" } })));
    const res = await loadEmbed(API, TOKEN, unsafe);
    expect(res.ok).toBe(false);
    const down = vi.fn(async () => Promise.reject(new TypeError("Failed to fetch")));
    const res2 = await loadEmbed(API, TOKEN, down);
    expect(res2.ok).toBe(false);
  });
});

describe("EmbedPage", () => {
  function page(body: unknown, opts: { renderer?: Renderer; frame?: { framed: boolean; ancestorOrigins: string[] | null; referrer: string }; path?: string } = {}) {
    const els = elements();
    const renderer: Renderer =
      opts.renderer ??
      (async (ctx) => {
        ctx.container.textContent = `rows=${ctx.datasets.fan?.length ?? 0}`;
        return { export: async () => "data:image/png;base64,AAAA", destroy: () => undefined };
      });
    const loadRenderer = vi.fn(async () => renderer);
    const fetchFn = vi.fn(async () => response(200, body));
    const p = new EmbedPage({
      apiUrl: API,
      pathname: opts.path ?? `/embed/${TOKEN}`,
      elements: els,
      theme: "light",
      loadRenderer,
      fetchFn: fetchFn,
      frame: opts.frame ?? { framed: false, ancestorOrigins: null, referrer: "" },
    });
    return { p, els, loadRenderer, fetchFn };
  }

  it("renders the plot with its title, caption, expiry and PNG fallback", async () => {
    const { p, els, loadRenderer } = page(view({ image_url: "https://storage.example/p.png?sig=1" }));
    await p.start();
    expect(p.state).toBe("ready");
    expect(loadRenderer).toHaveBeenCalledWith("vega-lite");
    expect(els.plot.textContent).toBe("rows=3");
    expect(els.title.textContent).toBe("EU response curves");
    expect(els.caption.textContent).toBe("Spend against expected revenue.");
    expect(els.meta.textContent).toContain("2027-01-13");
    expect(els.png.hidden).toBe(false);
    expect(els.png.href).toBe("https://storage.example/p.png?sig=1");
    expect(els.status.textContent).toBe("");
  });

  it("shows no PNG link without an image and no markup from untrusted text", async () => {
    const { p, els } = page(view({ title: "<b>bold</b>" }));
    await p.start();
    expect(els.png.hidden).toBe(true);
    expect(els.title.textContent).toBe("<b>bold</b>");
    expect(els.title.children).toHaveLength(0);
  });

  it("refuses to render on a site the link was not shared with", async () => {
    const { p, els, loadRenderer } = page(view({ allowed_origins: ["https://acme.notion.site"] }), {
      frame: { framed: true, ancestorOrigins: ["https://evil.example"], referrer: "" },
    });
    await p.start();
    expect(p.state).toBe("error");
    expect(els.status.textContent).toMatch(/only be shown on the sites/);
    expect(loadRenderer).not.toHaveBeenCalled();
    expect(els.title.textContent).toBe("");
  });

  it("does not fetch for a malformed path", async () => {
    const { p, els, fetchFn } = page(view(), { path: "/embed/../../v1/me" });
    await p.start();
    expect(fetchFn).not.toHaveBeenCalled();
    expect(els.status.textContent).toMatch(/does not exist/);
  });

  it("falls back to the PNG link when drawing fails", async () => {
    const failing: Renderer = async () => Promise.reject(new Error("boom"));
    const { p, els } = page(view({ image_url: "https://storage.example/p.png" }), { renderer: failing });
    await p.start();
    expect(p.state).toBe("error");
    expect(els.status.textContent).toMatch(/PNG link below/);
    expect(els.png.hidden).toBe(false);
  });
});

describe("embed CSP", () => {
  it("extends connect-src to the API's embed path only", () => {
    const csp = buildEmbedCsp({ apiUrl: API });
    expect(csp).toContain(`connect-src 'self' ${API}/v1/embeds/`);
    expect(csp).toContain("default-src 'none'");
    expect(csp).toContain("script-src 'self';");
    expect(csp).not.toContain("unsafe-eval");
    expect(csp).not.toContain("frame-ancestors");
    expect(buildCsp({ appOrigins: [] })).toContain("connect-src 'self';");
  });

  it("lets https sites frame the embed page through the header", () => {
    const headers = embedHeaders({ apiUrl: API, frameOrigins: ["http://127.0.0.1:5173"] });
    expect(headers["Content-Security-Policy"]).toMatch(/frame-ancestors 'self' https: http:\/\/127\.0\.0\.1:5173$/);
    expect(headers["Referrer-Policy"]).toBe("no-referrer");
    expect(headers["X-Robots-Tag"]).toContain("noindex");
  });

  it("validates the API origin", () => {
    expect(parseApiUrl(undefined, "http://127.0.0.1:8000")).toBe("http://127.0.0.1:8000");
    expect(parseApiUrl("https://api.example/", "x")).toBe("https://api.example");
    expect(parseApiUrl(API_URL_PLACEHOLDER, "x")).toBe(API_URL_PLACEHOLDER);
    expect(() => parseApiUrl("https://api.example/v1", "x")).toThrow();
    expect(() => parseApiUrl("javascript:alert(1)", "x")).toThrow();
  });
});

describe("embed page unfurl", () => {
  it("unfurls as a titled card in chat tools, naming no plot and no token", async () => {
    const fs = await import("node:fs");
    const path = await import("node:path");
    const html = fs.readFileSync(path.join(process.cwd(), "embed.html"), "utf-8"); // vitest runs in the package
    const meta = (property: string) => new RegExp(`<meta property="${property}" content="([^"]+)"`).exec(html)?.[1];
    // Chat tools (Slack, Teams) do not frame embeds: they show these tags. Static on purpose:
    // the page is the same for every link, so the card names neither the plot nor the token.
    expect(meta("og:title")).toBe("Shared chart");
    expect(meta("og:site_name")).toBe("modeling-api");
    expect(meta("og:description")).toMatch(/read-only chart.*open the link/i);
    expect(html).not.toMatch(/mpe_[0-9a-z]/);
  });
});
