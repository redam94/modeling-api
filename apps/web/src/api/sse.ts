/**
 * Server-sent events for `GET /v1/runs/{run_id}/events` (see docs/reference/http-api.md).
 *
 * Uses fetch + a streaming parser instead of EventSource so the client controls
 * `Last-Event-ID` on every (re)connect, can abort, and can stop for good on `end`.
 */
import { env } from "@/env";
import { useAuth } from "@/stores/auth";

import { parseTrackingFrame, type TrackingFrame } from "./tracking";
import type { Run, RunEventView } from "./types";

export interface SseFrame {
  event: string;
  data: string;
  id: string | null;
  retry: number | null;
}

/** Incremental parser for the `text/event-stream` format (WHATWG HTML §9.2). */
export class SseParser {
  private buffer = "";
  private event = "";
  private data: string[] = [];
  private id: string | null = null;
  private retry: number | null = null;
  private sawField = false;

  /** Feed a decoded chunk; returns the frames it completed. */
  feed(chunk: string): SseFrame[] {
    this.buffer += chunk;
    const frames: SseFrame[] = [];
    for (;;) {
      const match = /\r\n|\r|\n/.exec(this.buffer);
      if (!match) break;
      // A lone trailing "\r" may be the first half of "\r\n": wait for more input.
      if (match[0] === "\r" && match.index === this.buffer.length - 1) break;
      const line = this.buffer.slice(0, match.index);
      this.buffer = this.buffer.slice(match.index + match[0].length);
      const frame = this.line(line);
      if (frame) frames.push(frame);
    }
    return frames;
  }

  private line(line: string): SseFrame | null {
    if (line === "") {
      if (!this.sawField) return null;
      const frame: SseFrame = {
        event: this.event || "message",
        data: this.data.join("\n"),
        id: this.id,
        retry: this.retry,
      };
      this.event = "";
      this.data = [];
      this.retry = null;
      this.sawField = false;
      // `id` persists across frames per the spec, but our frames carry their own id.
      this.id = null;
      return frame;
    }
    if (line.startsWith(":")) return null; // comment / keepalive
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    switch (field) {
      case "event":
        this.event = value;
        break;
      case "data":
        this.data.push(value);
        break;
      case "id":
        if (!value.includes("\u0000")) this.id = value;
        break;
      case "retry":
        if (/^\d+$/.test(value)) this.retry = Number(value);
        break;
      default:
        return null;
    }
    this.sawField = true;
    return null;
  }
}

/**
 * A run event as it arrives over SSE. Persisted events carry their `run_events` row `id`;
 * live `progress`/`log` frames (the server's `_live_json`) are not persisted and have none.
 */
export type RunStreamEvent = Omit<RunEventView, "id"> & { id: number | null };

/** Live frame types the server sends without an id (never persisted to `run_events`). */
const LIVE_EVENT_NAMES = new Set(["progress", "log"]);

export type RunStreamStatus = "connecting" | "open" | "reconnecting" | "ended" | "error";

export interface RunStreamHandlers {
  onSnapshot(run: Run): void;
  onEvent(event: RunStreamEvent, live: boolean): void;
  onEnd(run: Run): void;
  onStatus?(status: RunStreamStatus, detail?: string): void;
  /** v0.11 live `tracking` frame (no id, never persisted): metric summaries changed, params/tags changed. */
  onTracking?(frame: TrackingFrame): void;
}

export interface RunStreamOptions {
  signal: AbortSignal;
  /** Resume after this persisted event id (normally the last one seen). */
  lastEventId?: number | null;
  fetchImpl?: typeof fetch;
  /** Extra request headers (the project selection, `X-Mapi-Project`). */
  headers?: Record<string, string>;
  /** Upper bound for the reconnect backoff in ms. */
  maxBackoffMs?: number;
  sleep?: (ms: number, signal: AbortSignal) => Promise<void>;
}

const RUN_EVENT_NAMES = new Set([
  "state",
  "progress",
  "log",
  "diagnostic",
  "warning",
  "checkpoint",
  "metric",
  "complete",
  "error",
]);

function defaultSleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const t = setTimeout(resolve, ms);
    signal.addEventListener(
      "abort",
      () => {
        clearTimeout(t);
        resolve();
      },
      { once: true },
    );
  });
}

function parseJson(data: string): unknown {
  try {
    return JSON.parse(data) as unknown;
  } catch {
    return undefined;
  }
}

function isRun(v: unknown): v is Run {
  return typeof v === "object" && v !== null && typeof (v as Run).id === "string" && typeof (v as Run).state === "string";
}

/**
 * Validate an event frame's JSON. Persisted events must carry a numeric `id`; live
 * `progress`/`log` frames come without one and are returned with `id: null`.
 */
export function parseRunStreamEvent(v: unknown): RunStreamEvent | null {
  if (typeof v !== "object" || v === null || Array.isArray(v)) return null;
  const e = v as Record<string, unknown>;
  if (
    typeof e.type !== "string" ||
    typeof e.seq !== "number" ||
    typeof e.run_id !== "string" ||
    typeof e.attempt !== "number" ||
    typeof e.ts !== "string"
  ) {
    return null;
  }
  const payload = typeof e.payload === "object" && e.payload !== null && !Array.isArray(e.payload) ? e.payload : {};
  let id: number | null;
  if (typeof e.id === "number") id = e.id;
  else if (e.id === undefined || e.id === null) {
    if (!LIVE_EVENT_NAMES.has(e.type)) return null; // persisted kinds always carry their id
    id = null;
  } else return null;
  return { ...(e as unknown as RunEventView), id, payload: payload as RunEventView["payload"] };
}

/**
 * Follow a run's event stream until the server sends `end` or `signal` aborts.
 * Reconnects with `Last-Event-ID` (server `retry:` hint, exponential backoff on errors);
 * stops for good on 404, 401 (raising the session-expired flag) and 403.
 */
export async function followRunEvents(
  runId: string,
  handlers: RunStreamHandlers,
  opts: RunStreamOptions,
): Promise<void> {
  const fetchImpl = opts.fetchImpl ?? fetch;
  const sleep = opts.sleep ?? defaultSleep;
  const maxBackoff = opts.maxBackoffMs ?? 30_000;
  let lastId = opts.lastEventId ?? null;
  let retryMs = 3000;
  let failures = 0;
  const url = `${env.apiBase}/v1/runs/${encodeURIComponent(runId)}/events`;

  while (!opts.signal.aborted) {
    handlers.onStatus?.(failures || lastId !== null ? "reconnecting" : "connecting");
    let ended = false;
    try {
      const headers: Record<string, string> = { ...opts.headers, Accept: "text/event-stream" };
      if (lastId !== null) headers["Last-Event-ID"] = String(lastId);
      const res = await fetchImpl(url, { headers, signal: opts.signal, credentials: "include", cache: "no-store" });
      if (res.status === 404) {
        handlers.onStatus?.("error", "run not found");
        return;
      }
      // The server closes a stream whose credential stopped granting access; the reconnect
      // then answers 401/403. Retrying cannot fix either: stop, and let the shell ask for a
      // new sign-in on 401 (like every other API call does).
      if (res.status === 401) {
        useAuth.getState().markExpired();
        handlers.onStatus?.("error", "your session has expired; sign in again");
        return;
      }
      if (res.status === 403) {
        handlers.onStatus?.("error", "you no longer have access to this run");
        return;
      }
      if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
      if (!(res.headers.get("content-type") ?? "").includes("text/event-stream")) {
        throw new Error("the server did not return an event stream");
      }
      handlers.onStatus?.("open");
      failures = 0;
      const parser = new SseParser();
      const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        for (const frame of parser.feed(value)) {
          if (frame.retry !== null) retryMs = Math.min(Math.max(frame.retry, 500), maxBackoff);
          const data = parseJson(frame.data);
          if (frame.event === "snapshot" && isRun(data)) {
            handlers.onSnapshot(data);
          } else if (frame.event === "end" && isRun(data)) {
            ended = true;
            handlers.onEnd(data);
          } else if (frame.event === "tracking") {
            // Live like progress/log: no id, so it never moves the resume point.
            const tracking = parseTrackingFrame(data);
            if (tracking) handlers.onTracking?.(tracking);
          } else if (RUN_EVENT_NAMES.has(frame.event)) {
            const ev = parseRunStreamEvent(data);
            if (!ev) continue;
            // Only frames with an `id:` line are persisted; live frames never move the resume point.
            const persisted = frame.id !== null && /^\d+$/.test(frame.id);
            if (persisted) lastId = Number(frame.id);
            handlers.onEvent(ev, true);
          }
        }
        if (ended) {
          await reader.cancel().catch(() => undefined);
          break;
        }
      }
    } catch (err) {
      if (opts.signal.aborted) return;
      failures += 1;
      handlers.onStatus?.("reconnecting", err instanceof Error ? err.message : String(err));
    }
    if (ended) {
      handlers.onStatus?.("ended");
      return;
    }
    const delay = failures ? Math.min(retryMs * 2 ** (failures - 1), maxBackoff) : retryMs;
    await sleep(delay, opts.signal);
  }
}
