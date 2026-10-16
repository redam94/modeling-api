/**
 * "Run exact", forecasts and scenario presets: compile the Guided request into a ScoreRequest
 * (`POST /v1/guided/requests:compile`), submit it (`Prefer: wait`), follow a run that outlives the
 * wait, collect the outputs by replaying the same Idempotency-Key, and read the Guided view the
 * score run wrote.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { api, newIdempotencyKey, unwrap } from "@/api/client";
import { useProjectScope } from "@/api/queries";
import type { ModelVersion, Run } from "@/api/types";
import { isTerminal } from "@/lib/run-state";

import { compileGuided, fetchScoreView, submitScore, type ScoreViewKind } from "./api";
import type { GuidedCompileBody } from "./types";
import type { CounterfactualView, ForecastView } from "./views";

export type ScoreJob =
  | { status: "idle" }
  | { status: "running"; run: Run | null }
  | { status: "done"; run: Run; forecast: ForecastView | null; counterfactual: CounterfactualView | null }
  | { status: "error"; error: unknown };

const POLL_MS = 1500;

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const t = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => {
      clearTimeout(t);
      reject(new DOMException("aborted", "AbortError"));
    });
  });
}

export function useScoreJob(mv: ModelVersion | null | undefined, kind: ScoreViewKind) {
  const scope = useProjectScope();
  const [job, setJob] = useState<ScoreJob>({ status: "idle" });
  const ctrl = useRef<AbortController | null>(null);
  useEffect(() => () => ctrl.current?.abort(), []);

  const run = useCallback(
    async (body: GuidedCompileBody, labels: Record<string, string> = {}) => {
      if (!mv) return;
      ctrl.current?.abort();
      const c = new AbortController();
      ctrl.current = c;
      setJob({ status: "running", run: null });
      try {
        const request = await compileGuided(body, scope.headers, c.signal);
        const key = newIdempotencyKey();
        let { result } = await submitScore(mv, request, { key, wait: 30, headers: scope.headers, labels });
        while (!result.done) {
          setJob({ status: "running", run: result.run });
          await sleep(POLL_MS, c.signal);
          const r = await unwrap(api.GET("/v1/runs/{run_id}", { params: { path: { run_id: result.run.id } }, headers: scope.headers, signal: c.signal }));
          if (isTerminal(r.state)) {
            if (r.state !== "succeeded") {
              result = { done: true, run: r, outputs: [] };
              break;
            }
            ({ result } = await submitScore(mv, request, { key, wait: 0, headers: scope.headers, labels }));
          }
        }
        if (result.run.state !== "succeeded") throw new Error(result.run.problem?.detail ?? result.run.problem?.title ?? "The run did not finish.");
        const view = await fetchScoreView(result.outputs ?? [], kind, c.signal);
        setJob({
          status: "done",
          run: result.run,
          forecast: view.kind === "forecast" ? view.view : null,
          counterfactual: view.kind === "counterfactual" ? view.view : null,
        });
      } catch (e) {
        if (e instanceof DOMException && e.name === "AbortError") return;
        setJob({ status: "error", error: e });
      }
    },
    [mv, scope.headers, kind],
  );

  const reset = useCallback(() => {
    ctrl.current?.abort();
    setJob({ status: "idle" });
  }, []);

  return { job, run, reset };
}
