import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useReducer } from "react";

import { qk } from "@/api/queries";
import { followRunEvents, type RunStreamStatus } from "@/api/sse";
import type { ProgressPayload, ProgressSnapshot, Run, RunEventView } from "@/api/types";
import { isTerminal } from "@/lib/run-state";

export interface RunStreamState {
  status: RunStreamStatus;
  detail: string | null;
  /** Persisted events (state, warning, diagnostic, checkpoint, metric, complete, error), by id. */
  events: RunEventView[];
  /** Latest live progress (not persisted server-side; the snapshot carries the last one). */
  progress: ProgressSnapshot | null;
}

type Action =
  | { type: "status"; status: RunStreamStatus; detail?: string | undefined }
  | { type: "event"; event: RunEventView }
  | { type: "progress"; progress: ProgressSnapshot }
  | { type: "reset" };

const MAX_EVENTS = 2000;

export function runStreamReducer(state: RunStreamState, action: Action): RunStreamState {
  switch (action.type) {
    case "status":
      return { ...state, status: action.status, detail: action.detail ?? null };
    case "progress":
      return { ...state, progress: action.progress };
    case "event": {
      if (state.events.some((e) => e.id === action.event.id)) return state; // replay dedupe
      const events = [...state.events, action.event].sort((a, b) => a.id - b.id);
      return { ...state, events: events.slice(-MAX_EVENTS) };
    }
    case "reset":
      return initial;
  }
}

const initial: RunStreamState = { status: "connecting", detail: null, events: [], progress: null };

export function progressFromEvent(e: RunEventView): ProgressSnapshot | null {
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
 * updates); persisted events accumulate here; progress frames update the live snapshot.
 * Does nothing for terminal runs beyond loading the persisted events once.
 */
export function useRunEvents(run: Run | undefined): RunStreamState {
  const qc = useQueryClient();
  const [state, dispatch] = useReducer(runStreamReducer, initial);
  const runId = run?.id;
  const terminal = run ? isTerminal(run.state) : false;

  useEffect(() => {
    dispatch({ type: "reset" });
  }, [runId]);

  useEffect(() => {
    if (!runId) return;
    const ctrl = new AbortController();
    const setRun = (r: Run) => {
      qc.setQueryData(qk.run(r.id), r);
      if (r.progress) dispatch({ type: "progress", progress: r.progress });
    };
    void followRunEvents(
      runId,
      {
        onSnapshot: setRun,
        onEnd: (r) => {
          setRun(r);
          void qc.invalidateQueries({ queryKey: ["runs", "list"] });
        },
        onEvent: (e) => {
          const progress = progressFromEvent(e);
          if (progress) {
            dispatch({ type: "progress", progress });
            return;
          }
          if (e.type === "log") return; // logs are paged from /logs
          dispatch({ type: "event", event: e });
          if (e.type === "state" || e.type === "complete" || e.type === "error") {
            void qc.invalidateQueries({ queryKey: qk.run(runId) });
          }
        },
        onStatus: (status, detail) => dispatch({ type: "status", status, detail }),
      },
      { signal: ctrl.signal },
    );
    return () => ctrl.abort();
    // `terminal` restarts nothing: a terminal run's stream replays its events then ends.
  }, [runId, qc]);

  return { ...state, status: terminal && state.status === "open" ? "ended" : state.status };
}
