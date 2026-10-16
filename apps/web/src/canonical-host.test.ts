import type { IncomingMessage, ServerResponse } from "node:http";

import { describe, expect, it, vi } from "vitest";

import { canonicalHostRedirect } from "../canonical-host";

function run(host: string | undefined, url = "/runs?state=running") {
  const headers: Record<string, string> = {};
  const res = {
    statusCode: 200,
    setHeader: (k: string, v: string) => void (headers[k] = v),
    end: vi.fn(),
  } as unknown as ServerResponse;
  const req = { headers: host === undefined ? {} : { host }, url } as IncomingMessage;
  const next = vi.fn();
  canonicalHostRedirect("127.0.0.1")(req, res, next);
  return { next, status: res.statusCode, location: headers.Location };
}

describe("canonicalHostRedirect", () => {
  it("sends localhost to the same port and path on 127.0.0.1", () => {
    const r = run("localhost:5173");
    expect(r.next).not.toHaveBeenCalled();
    expect(r.status).toBe(307);
    expect(r.location).toBe("http://127.0.0.1:5173/runs?state=running");
  });

  it("passes the canonical host and unknown hosts through", () => {
    expect(run("127.0.0.1:5173").next).toHaveBeenCalledOnce();
    expect(run("app.example.com").next).toHaveBeenCalledOnce();
    expect(run(undefined).next).toHaveBeenCalledOnce();
  });
});
