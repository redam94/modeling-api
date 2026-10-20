import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useReducer, useRef } from "react";

import { qk, useProjectScope } from "@/api/queries";
import { followRunEvents, type RunStreamEvent, type RunStreamStatus } from "@/api/sse";
import type { TrackingFrame } from "@/api/tracking";
import type { ProgressPayload, ProgressSnapshot, Run, RunEventView } from "@/api/types";
import { isTerminal } from "@/lib/run-state";

export interface RunStreamState {
  status: RunStreamStatus;
  detail: string | null;
  /** Persisted events (state, warning, diagnostic, checkpoint, metric, complete, error), by id. */
  events: RunEventView[];
  /** Latest live progress (not persisted server-side; the snapshot carries the last one). */
  progress: ProgressSnapshot | null;
  /** v0.11: live `tracking` frames seen (they have no id and are never kept in `events`). */
  trackingTick: number;
  /** Metric keys the latest `tracking` frame changed. */
  changedKeys: string[];
}

type Action =
  | { type: "status"; status: RunStreamStatus; detail?: string | undefined }
  | { type: "event"; event: RunEventView }
  | { type: "progress"; progress: ProgressSnapshot }
  | { type: "tracking"; frame: TrackingFrame }
  | { type: "reset" };

const MAX_EVENTS = 2000;

export function runStreamReducer(state: RunStreamState, action: Action): RunStreamState {
  switch (action.type) {
    case "status":
      return { ...state, status: action.status, detail: action.detail ?? null };
    case "progress":
      return { ...state, progress: action.progress };
    case "tracking":
      return { ...state, trackingTick: state.trackingTick + 1, changedKeys: trackingKeys(action.frame) };
    case "event": {
      if (state.events.some((e) => e.id === action.event.id)) return state; // replay dedupe
      const events = [...state.events, action.event].sort((a, b) => a.id - b.id);
      return { ...state, events: events.slice(-MAX_EVENTS) };
    }
    case "reset":
      return initial;
  }
}

const initial: RunStreamState = { status: "connecting", detail: null, events: [], progress: null, trackingTick: 0, changedKeys: [] };

/** The distinct metric keys of a `tracking` frame, sorted. */
export function trackingKeys(frame: TrackingFrame): string[] {
  return [...new Set(frame.series.map((s) => s.key))].sort();
}

/** What changed since the last refetch: accumulated over the `tracking` frames of one throttle window. */
export interface TrackingChanges {
  keys: Set<string>;
  params: boolean;
  tags: boolean;
}

export function mergeTracking(prev: TrackingChanges | null, frame: TrackingFrame): TrackingChanges {
  const keys = new Set(prev?.keys);
  for (const k of trackingKeys(frame)) keys.add(k);
  return { keys, params: Boolean(prev?.params) || frame.params, tags: Boolean(prev?.tags) || frame.tags };
}

/** Tracking refetches happen at most this often (a fit can send a frame every second). */
export const TRACKING_REFETCH_MS = 2000;

export function progressFromEvent(e: RunStreamEvent): ProgressSnapshot | null {
  if (e.type !== "progress") return null;
  const p = e.payload as Partial<ProgressPayload>;
  if (typeof p.phase !== "string") return null;
  return {
    seq: e.seq,
    phase: p.phase,
    fraction: typeof p.fraction === "number" ? p.fraction : null,
    message: typeof p.message === "string" ? p.message : null,
    eta_seconds: typeof p.eta_seconds === "number" ? p.eta_seconds : null,
    chains: Array.isArray(p.chains) ? p.chains : [],
    updated_at: e.ts,
  };
}

/**
 * Follow a run over SSE. Snapshot/end frames refresh the cached `Run` (so every view of it
 * updates); persisted events accumulate here; progress frames update the live snapshot;
 * `tracking` frames refetch the changed metrics, params and tags (at most every 2 s).
 * Does nothing for terminal runs beyond loading the persisted events once.
 */
export function useRunEvents(run: Run | undefined): RunStreamState {
  const qc = useQueryClient();
  const scope = useProjectScope();
  const [state, dispatch] = useReducer(runStreamReducer, initial);
  const runId = run?.id;
  const terminal = run ? isTerminal(run.state) : false;
  const pending = useRef<TrackingChanges | null>(null);

  useEffect(() => {
    dispatch({ type: "reset" });
  }, [runId]);

  useEffect(() => {
    if (!runId) return;
    const ctrl = new AbortController();
    let timer: ReturnType<typeof setTimeout> | null = null;
    let lastFlush = 0;
    const flush = () => {
      timer = null;
      lastFlush = Date.now();
      const c = pending.current;
      pending.current = null;
      if (!c) return;
      if (c.keys.size) void qc.invalidateQueries({ queryKey: qk.runMetrics(scope.key, runId) });
      for (const key of c.keys) void qc.invalidateQueries({ queryKey: qk.metricHistory(scope.key, runId, key) });
      if (c.params) void qc.invalidateQueries({ queryKey: qk.runParams(scope.key, runId) });
      if (c.tags) void qc.invalidateQueries({ queryKey: qk.run(scope.key, runId), exact: true });
    };
    const setRun = (r: Run) => {
      qc.setQueryData(qk.run(scope.key, r.id), r);
      if (r.progress) dispatch({ type: "progress", progress: r.progress });
    };
    void followRunEvents(
      runId,
      {
        onSnapshot: setRun,
        onEnd: (r) => {
          setRun(r);
          void qc.invalidateQueries({ queryKey: qk.runLists(scope.key) });
          // The runner flushes its last metric batch before the run ends.
          void qc.invalidateQueries({ queryKey: qk.runMetricData(scope.key, runId) });
        },
        onTracking: (frame) => {
          if (frame.run_id !== runId) return;
          dispatch({ type: "tracking", frame });
          pending.current = mergeTracking(pending.current, frame);
          if (timer) return;
          timer = setTimeout(flush, Math.max(0, lastFlush + TRACKING_REFETCH_MS - Date.now()));
        },
        onEvent: (e) => {
          const progress = progressFromEvent(e);
          if (progress) {
            dispatch({ type: "progress", progress });
            return;
          }
          if (e.type === "log") return; // logs are paged from /logs
          if (e.id === null) return; // only persisted events are kept (and deduped by id)
          dispatch({ type: "event", event: { ...e, id: e.id } });
          if (e.type === "state" || e.type === "complete" || e.type === "error") {
            void qc.invalidateQueries({ queryKey: qk.run(scope.key, runId) });
          }
        },
        onStatus: (status, detail) => dispatch({ type: "status", status, detail }),
      },
      { signal: ctrl.signal, headers: scope.headers },
    );
    return () => {
      ctrl.abort();
      if (timer) clearTimeout(timer);
      pending.current = null;
    };
    // `terminal` restarts nothing: a terminal run's stream replays its events then ends.
  }, [runId, qc, scope]);

  return { ...state, status: terminal && state.status === "open" ? "ended" : state.status };
}
