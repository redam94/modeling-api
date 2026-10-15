import { describe, expect, it, vi } from "vitest";

import type { Run, RunEventView } from "./types";

import { followRunEvents, SseParser } from "./sse";

describe("SseParser", () => {
  it("parses frames split across chunks with CRLF, comments, ids and retry", () => {
    const p = new SseParser();
    expect(p.feed("retry: 3000\r\nevent: snap")).toEqual([]);
    const frames = p.feed('shot\r\ndata: {"a":1}\r\n\r\n: keepalive\n\nid: 7\nevent: state\ndata: x\ndata: y\n\n');
    expect(frames).toEqual([
      { event: "snapshot", data: '{"a":1}', id: null, retry: 3000 },
      { event: "state", data: "x\ny", id: "7", retry: null },
    ]);
  });

  it("waits for the second half of a CRLF split across chunks", () => {
    const p = new SseParser();
    expect(p.feed("data: a\r")).toEqual([]);
    expect(p.feed("\n\r\n")).toEqual([{ event: "message", data: "a", id: null, retry: null }]);
  });

  it("ignores unknown fields and frames without fields", () => {
    const p = new SseParser();
    expect(p.feed("foo: bar\n\n\n")).toEqual([]);
  });
});

const run = (state: Run["state"]): Run =>
  ({ id: "run_1", state, kind: "fit", attempt: 1, resource_class: "S", timeout: "1m", tenant_id: "default", project_id: "default", created_at: "", updated_at: "" });

const event = (id: number): RunEventView => ({ id, run_id: "run_1", attempt: 1, seq: id, ts: "2026-10-15T09:30:00Z", type: "state", payload: { phase: "fit" } });

function sse(body: string, status = 200): Response {
  return new Response(body, { status, headers: { "content-type": "text/event-stream" } });
}

const frame = (name: string, data: unknown, id?: number) => `${id !== undefined ? `id: ${id}\n` : ""}event: ${name}\ndata: ${JSON.stringify(data)}\n\n`;

describe("followRunEvents", () => {
  it("reconnects with Last-Event-ID after a dropped stream and stops at end", async () => {
    const calls: (string | null)[] = [];
    const responses = [
      sse(frame("snapshot", run("running")) + frame("state", event(1), 1) + frame("state", event(2), 2)),
      sse(frame("snapshot", run("running")) + frame("state", event(3), 3) + frame("end", run("succeeded"))),
    ];
    const fetchImpl = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
      calls.push(new Headers(init?.headers).get("Last-Event-ID"));
      return responses.shift() ?? sse("", 500);
    });
    const onEvent = vi.fn();
    const onEnd = vi.fn();
    const onSnapshot = vi.fn();
    await followRunEvents(
      "run_1",
      { onSnapshot, onEvent, onEnd },
      { signal: new AbortController().signal, fetchImpl: fetchImpl, sleep: async () => undefined },
    );
    expect(calls).toEqual([null, "2"]);
    expect(onEvent.mock.calls.map((c) => (c[0] as RunEventView).id)).toEqual([1, 2, 3]);
    expect(onSnapshot).toHaveBeenCalledTimes(2);
    expect(onEnd).toHaveBeenCalledWith(expect.objectContaining({ state: "succeeded" }));
  });

  it("does not advance Last-Event-ID for unpersisted (id-less) frames", async () => {
    const calls: (string | null)[] = [];
    const responses = [
      sse(frame("state", event(5), 5) + frame("progress", { ...event(0), type: "progress" })),
      sse(frame("end", run("failed"))),
    ];
    const fetchImpl = vi.fn(async (_u: RequestInfo | URL, init?: RequestInit) => {
      calls.push(new Headers(init?.headers).get("Last-Event-ID"));
      return responses.shift()!;
    });
    await followRunEvents("run_1", { onSnapshot: vi.fn(), onEvent: vi.fn(), onEnd: vi.fn() }, {
      signal: new AbortController().signal,
      fetchImpl: fetchImpl,
      sleep: async () => undefined,
    });
    expect(calls).toEqual([null, "5"]);
  });

  it("backs off on errors and gives up on 404", async () => {
    const delays: number[] = [];
    const responses = [new Response("x", { status: 502 }), new Response("x", { status: 502 }), new Response("nope", { status: 404 })];
    const onStatus = vi.fn();
    await followRunEvents("run_1", { onSnapshot: vi.fn(), onEvent: vi.fn(), onEnd: vi.fn(), onStatus }, {
      signal: new AbortController().signal,
      fetchImpl: (async () => responses.shift()!),
      sleep: async (ms) => {
        delays.push(ms);
      },
    });
    expect(delays).toEqual([3000, 6000]);
    expect(onStatus).toHaveBeenLastCalledWith("error", "run not found");
  });

  it("refuses a non-SSE response body", async () => {
    const ctrl = new AbortController();
    const onStatus = vi.fn((s: string) => {
      if (s === "reconnecting") ctrl.abort();
    });
    await followRunEvents("run_1", { onSnapshot: vi.fn(), onEvent: vi.fn(), onEnd: vi.fn(), onStatus }, {
      signal: ctrl.signal,
      fetchImpl: (async () => new Response("{}", { headers: { "content-type": "application/json" } })),
      sleep: async () => undefined,
    });
    expect(onStatus).toHaveBeenCalledWith("reconnecting", "the server did not return an event stream");
  });
});
