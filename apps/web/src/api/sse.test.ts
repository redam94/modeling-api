import { describe, expect, it, vi } from "vitest";

import { useAuth } from "@/stores/auth";

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

/** A live frame exactly as the server's `_live_json` writes it: no `id` field and no `id:` line. */
const live = (seq: number, type: "progress" | "log", payload: Record<string, unknown> = { phase: "sample" }) => ({
  run_id: "run_1",
  attempt: 1,
  seq,
  ts: "2026-10-15T09:30:01Z",
  type,
  payload,
});

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
      sse(frame("state", event(5), 5) + frame("progress", live(9, "progress"))),
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

  it("delivers the server's live progress and log frames (which carry no id)", async () => {
    const calls: (string | null)[] = [];
    const responses = [
      sse(
        frame("snapshot", run("running")) +
          frame("state", event(4), 4) +
          frame("progress", live(10, "progress", { phase: "sample", fraction: 0.25 })) +
          frame("log", live(11, "log", { stream: "stdout", line: "hello" })) +
          frame("progress", { seq: 12, type: "progress" }) + // no run_id/attempt/ts: malformed, dropped
          frame("end", run("succeeded")),
      ),
    ];
    const fetchImpl = vi.fn(async (_u: RequestInfo | URL, init?: RequestInit) => {
      calls.push(new Headers(init?.headers).get("Last-Event-ID"));
      return responses.shift()!;
    });
    const onEvent = vi.fn();
    await followRunEvents("run_1", { onSnapshot: vi.fn(), onEvent, onEnd: vi.fn() }, {
      signal: new AbortController().signal,
      fetchImpl,
      sleep: async () => undefined,
    });
    const got = onEvent.mock.calls.map((c) => c[0] as { id: number | null; type: string; seq: number; payload: unknown });
    expect(got.map((e) => [e.type, e.id, e.seq])).toEqual([
      ["state", 4, 4],
      ["progress", null, 10],
      ["log", null, 11],
    ]);
    expect(got[1]!.payload).toEqual({ phase: "sample", fraction: 0.25 });
    expect(calls).toEqual([null]);
  });

  it("hands v0.11 tracking frames to onTracking, never to onEvent, without moving the resume point", async () => {
    const calls: (string | null)[] = [];
    const summary = { key: "step_size", labels: { chain: "0" }, attempt: 1, count: 3, last: 0.12, last_step: 30, min: 0.12, max: 0.6, first_ts: "t", last_ts: "t" };
    const responses = [
      sse(frame("state", event(6), 6) + frame("tracking", { run_id: "run_1", series: [summary], params: true, tags: false, media: true }) + frame("tracking", { series: [] })),
      sse(frame("end", run("succeeded"))),
    ];
    const fetchImpl = vi.fn(async (_u: RequestInfo | URL, init?: RequestInit) => {
      calls.push(new Headers(init?.headers).get("Last-Event-ID"));
      return responses.shift()!;
    });
    const onEvent = vi.fn();
    const onTracking = vi.fn();
    await followRunEvents("run_1", { onSnapshot: vi.fn(), onEvent, onEnd: vi.fn(), onTracking }, {
      signal: new AbortController().signal,
      fetchImpl,
      sleep: async () => undefined,
    });
    expect(onEvent.mock.calls.map((c) => (c[0] as RunEventView).id)).toEqual([6]);
    expect(onTracking).toHaveBeenCalledTimes(1); // the malformed frame (no run_id) is dropped
    expect(onTracking).toHaveBeenCalledWith({ run_id: "run_1", series: [summary], params: true, tags: false, media: true });
    expect(calls).toEqual([null, "6"]);
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

  it("stops on 401 and raises the session-expired flag instead of retrying forever", async () => {
    useAuth.setState({ mode: "session", expired: false });
    const delays: number[] = [];
    const responses = [sse(frame("snapshot", run("running"))), new Response("{}", { status: 401 })];
    const fetchImpl = vi.fn(async () => responses.shift() ?? sse("", 500));
    const onStatus = vi.fn();
    await followRunEvents("run_1", { onSnapshot: vi.fn(), onEvent: vi.fn(), onEnd: vi.fn(), onStatus }, {
      signal: new AbortController().signal,
      fetchImpl,
      sleep: async (ms) => {
        delays.push(ms);
      },
    });
    expect(fetchImpl).toHaveBeenCalledTimes(2);
    expect(onStatus).toHaveBeenLastCalledWith("error", "your session has expired; sign in again");
    expect(useAuth.getState().expired).toBe(true);
    useAuth.setState({ expired: false });
  });

  it("stops on 403 (access to the run was revoked)", async () => {
    const fetchImpl = vi.fn(async () => new Response("{}", { status: 403 }));
    const onStatus = vi.fn();
    await followRunEvents("run_1", { onSnapshot: vi.fn(), onEvent: vi.fn(), onEnd: vi.fn(), onStatus }, {
      signal: new AbortController().signal,
      fetchImpl,
      sleep: async () => undefined,
    });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(onStatus).toHaveBeenLastCalledWith("error", "you no longer have access to this run");
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
