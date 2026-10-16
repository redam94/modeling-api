/**
 * Live workflow-run events: `GET /v1/workflow-runs/{run_id}/events` with
 * `Accept: text/event-stream` sends a `snapshot` (the run), one frame per persisted event
 * (`event:` is the event type, `id:` its row id) and `end` when the run is finished. Reconnects
 * resume with `Last-Event-ID`, so no event is lost or replayed twice.
 */
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useReducer } from "react";

import { useProjectScope } from "@/api/queries";
import { SseParser, type RunStreamStatus } from "@/api/sse";
import { env } from "@/env";
import { useAuth } from "@/stores/auth";

import { TERMINAL_RUN, wk } from "./api";
import type { NodeRunStatus, WorkflowEvent, WorkflowRun } from "./types";

export interface WorkflowStreamHandlers {
  onSnapshot(run: WorkflowRun): void;
  onEvent(event: WorkflowEvent, id: number): void;
  onEnd(status: string | null): void;
  onStatus?(status: RunStreamStatus, detail?: string): void;
}

export interface WorkflowStreamOptions {
  signal: AbortSignal;
  lastEventId?: number | null;
  headers?: Record<string, string>;
  fetchImpl?: typeof fetch;
  maxBackoffMs?: number;
  sleep?: (ms: number, signal: AbortSignal) => Promise<void>;
}

function defaultSleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const t = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => {
      clearTimeout(t);
      resolve();
    }, { once: true });
  });
}

function parse(data: string): unknown {
  try {
    return JSON.parse(data) as unknown;
  } catch {
    return undefined;
  }
}

function isRun(v: unknown): v is WorkflowRun {
  return typeof v === "object" && v !== null && typeof (v as WorkflowRun).run_id === "string" && typeof (v as WorkflowRun).status === "string" && Array.isArray((v as WorkflowRun).nodes);
}

export function parseWorkflowEvent(v: unknown): WorkflowEvent | null {
  if (typeof v !== "object" || v === null || Array.isArray(v)) return null;
  const e = v as Record<string, unknown>;
  if (typeof e.type !== "string" || !e.type.startsWith("workflow.") || typeof e.seq !== "number" || typeof e.run_id !== "string" || typeof e.ts !== "string") return null;
  const data = typeof e.data === "object" && e.data !== null && !Array.isArray(e.data) ? (e.data as WorkflowEvent["data"]) : {};
  return { ...(e as unknown as WorkflowEvent), node_id: typeof e.node_id === "string" ? e.node_id : null, data };
}

/** Follow a workflow run until `end`, a refusal (401/403/404) or `signal` aborts. */
export async function followWorkflowRun(runId: string, handlers: WorkflowStreamHandlers, opts: WorkflowStreamOptions): Promise<void> {
  const fetchImpl = opts.fetchImpl ?? fetch;
  const sleep = opts.sleep ?? defaultSleep;
  const maxBackoff = opts.maxBackoffMs ?? 30_000;
  let lastId = opts.lastEventId ?? null;
  let retryMs = 3000;
  let failures = 0;
  const url = `${env.apiBase}/v1/workflow-runs/${encodeURIComponent(runId)}/events`;
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
      if (!(res.headers.get("content-type") ?? "").includes("text/event-stream")) throw new Error("the server did not return an event stream");
      handlers.onStatus?.("open");
      failures = 0;
      const parser = new SseParser();
      const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        for (const frame of parser.feed(value)) {
          if (frame.retry !== null) retryMs = Math.min(Math.max(frame.retry, 500), maxBackoff);
          const data = parse(frame.data);
          if (frame.event === "snapshot" && isRun(data)) handlers.onSnapshot(data);
          else if (frame.event === "end") {
            ended = true;
            const status = typeof data === "object" && data !== null && typeof (data as { status?: unknown }).status === "string" ? (data as { status: string }).status : null;
            handlers.onEnd(status);
          } else if (frame.id !== null && /^\d+$/.test(frame.id)) {
            const ev = parseWorkflowEvent(data);
            if (!ev) continue;
            lastId = Number(frame.id);
            handlers.onEvent(ev, lastId);
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
    await sleep(failures ? Math.min(retryMs * 2 ** (failures - 1), maxBackoff) : retryMs, opts.signal);
  }
}

/** The node status an event moves a node to (the run view refreshes the rest). */
export function statusFromEvent(type: WorkflowEvent["type"]): NodeRunStatus | null {
  switch (type) {
    case "workflow.node.queued":
      return "queued";
    case "workflow.node.started":
    case "workflow.node.retrying":
      return "running";
    case "workflow.node.succeeded":
      return "succeeded";
    case "workflow.node.cached":
      return "cached";
    case "workflow.node.skipped":
      return "skipped";
    case "workflow.node.failed":
      return "failed";
    default:
      return null;
  }
}

export interface WorkflowStreamState {
  status: RunStreamStatus;
  detail: string | null;
  events: (WorkflowEvent & { id: number })[];
}

type Action = { type: "status"; status: RunStreamStatus; detail?: string | undefined } | { type: "event"; event: WorkflowEvent & { id: number } } | { type: "reset" };

const initial: WorkflowStreamState = { status: "connecting", detail: null, events: [] };

export function workflowStreamReducer(state: WorkflowStreamState, action: Action): WorkflowStreamState {
  switch (action.type) {
    case "status":
      return { ...state, status: action.status, detail: action.detail ?? null };
    case "event":
      if (state.events.some((e) => e.id === action.event.id)) return state;
      return { ...state, events: [...state.events, action.event].sort((a, b) => a.id - b.id).slice(-2000) };
    case "reset":
      return initial;
  }
}

/** Apply an event to the cached run, so every view of it updates before the next refetch. */
export function applyEvent(run: WorkflowRun, event: WorkflowEvent): WorkflowRun {
  const next = statusFromEvent(event.type);
  let status = run.status;
  if (event.type === "workflow.run.started") status = "running";
  if (event.type === "workflow.run.succeeded") status = "succeeded";
  if (event.type === "workflow.run.failed") status = "failed";
  if (event.type === "workflow.run.cancelled") status = "cancelled";
  if (!next || !event.node_id) return status === run.status ? run : { ...run, status };
  const nodes = run.nodes ?? [];
  const found = nodes.some((n) => n.node_id === event.node_id);
  const updated = found
    ? nodes.map((n) => (n.node_id === event.node_id ? { ...n, status: next, ...(next === "running" && !n.started_at ? { started_at: event.ts } : {}) } : n))
    : nodes;
  return { ...run, status: status === "queued" ? "running" : status, nodes: updated };
}

/**
 * Follow a workflow run: the snapshot replaces the cached run, events patch node statuses at
 * once and refetch the run (results, decisions, outputs) shortly after.
 */
export function useWorkflowRunEvents(run: WorkflowRun | undefined): WorkflowStreamState {
  const qc = useQueryClient();
  const scope = useProjectScope();
  const [state, dispatch] = useReducer(workflowStreamReducer, initial);
  const runId = run?.run_id;
  const terminal = run ? TERMINAL_RUN.has(run.status) : false;

  useEffect(() => {
    dispatch({ type: "reset" });
  }, [runId]);

  useEffect(() => {
    if (!runId) return;
    const ctrl = new AbortController();
    const key = wk.run(scope.key, runId);
    let refetch: ReturnType<typeof setTimeout> | null = null;
    const scheduleRefetch = (ms = 400) => {
      if (refetch) return;
      refetch = setTimeout(() => {
        refetch = null;
        void qc.invalidateQueries({ queryKey: key, exact: true });
      }, ms);
    };
    void followWorkflowRun(
      runId,
      {
        onSnapshot: (r) => qc.setQueryData(key, r),
        onEvent: (e, id) => {
          dispatch({ type: "event", event: { ...e, id } });
          qc.setQueryData<WorkflowRun>(key, (old) => (old ? applyEvent(old, e) : old));
          if (e.type !== "workflow.node.queued" && e.type !== "workflow.node.started") scheduleRefetch();
        },
        onEnd: () => {
          scheduleRefetch(0);
          void qc.invalidateQueries({ queryKey: wk.runs(scope.key, run?.workflow_id ?? "") });
        },
        onStatus: (status, detail) => dispatch({ type: "status", status, detail }),
      },
      { signal: ctrl.signal, headers: scope.headers },
    );
    return () => {
      ctrl.abort();
      if (refetch) clearTimeout(refetch);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- one stream per run
  }, [runId, qc, scope]);

  return { ...state, status: terminal && state.status === "open" ? "ended" : state.status };
}
