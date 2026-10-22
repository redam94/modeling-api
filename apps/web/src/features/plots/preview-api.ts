/**
 * Live recipe previews (v0.18 D21 (ii), I-5): `POST /v1/catalog/plots/{id}/{version}:preview`
 * draws a plot recipe's example with other settings in a RENDER run; `GET
 * /v1/catalog/plots/previews/{run_id}` reads the result.
 *
 * `title`, `width` and `height` never need the server: {@link applyInstantSettings} changes
 * them in the browser. Every other setting needs a run, which takes about 10 s on a laptop
 * (WP0 probe P11) — show a pending state, never promise "instant". The server answers `200`
 * when the plot is ready (or cached for 7 days), else `202` with a `run_id` this module
 * polls. When the operator turned previews off the server answers `409 PREVIEW_DISABLED`:
 * {@link isPreviewDisabled} switches the playground to "Apply" mode.
 *
 * The route acts in the selected project (its own rate limits: 20 a minute per user, 200 a
 * day per tenant), so requests carry `X-Mapi-Project` explicitly: the client middleware
 * does not add it to `/v1/catalog` routes.
 */
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, ApiError, unwrapWithStatus, unwrap } from "@/api/client";
import { useProjectScope, type ProjectScope } from "@/api/queries";
import type { components } from "@/api/schema";
import type { PlotSpec, Problem } from "@/api/types";
import { emitOnboarding } from "@/lib/onboarding-events";

export type RecipePreviewResult = Omit<components["schemas"]["RecipePreviewResult"], "plot"> & { plot?: PlotSpec | null };

/** Settings the browser applies itself (`RecipeSettings`: title ≤ 300, width 80-2000, height 60-2000). */
export const INSTANT_SETTINGS = ["title", "width", "height"] as const;
export type InstantSetting = (typeof INSTANT_SETTINGS)[number];

/** How long the server may hold the request (`Prefer: wait=N`, at most 15). */
export const PREVIEW_WAIT_S = 12;
/** Poll period of a `202` preview, and the time after which the page gives up waiting. */
export const PREVIEW_POLL_MS = 1000;
export const PREVIEW_GIVE_UP_MS = 120_000;
/** The debounce the playground applies before asking (D21). */
export const PREVIEW_DEBOUNCE_MS = 700;

type Settings = Record<string, unknown>;

const INSTANT: ReadonlySet<string> = new Set(INSTANT_SETTINGS);

/** Split settings into the instant ones and the ones that need a run. */
export function splitSettings(settings: Settings): { instant: Partial<Record<InstantSetting, unknown>>; server: Settings } {
  const instant: Partial<Record<InstantSetting, unknown>> = {};
  const server: Settings = {};
  for (const [k, v] of Object.entries(settings)) {
    if (v === undefined) continue;
    if (INSTANT.has(k)) instant[k as InstantSetting] = v;
    else server[k] = v;
  }
  return { instant, server };
}

/** A stable key of the settings that need a run (sorted keys; instant ones left out). */
export function serverSettingsKey(settings: Settings): string {
  const { server } = splitSettings(settings);
  return JSON.stringify(Object.keys(server).sort().map((k) => [k, server[k]]));
}

function clampInt(v: unknown, lo: number, hi: number): number | null {
  return typeof v === "number" && Number.isFinite(v) ? Math.max(lo, Math.min(hi, Math.round(v))) : null;
}

/**
 * `plot` with the instant settings applied in the browser: the title (≤ 300 chars) and the
 * view size (on the inner `spec` of a faceted chart, else on the top level; concatenated
 * charts keep their sizes). Returns a new object; `plot` is not changed.
 */
export function applyInstantSettings(plot: PlotSpec, settings: Settings): PlotSpec {
  const { instant } = splitSettings(settings);
  const spec: Record<string, unknown> = { ...(plot.spec as Record<string, unknown>) };
  const out: PlotSpec = { ...plot, spec: spec };
  if (typeof instant.title === "string" && instant.title.trim()) {
    const title = instant.title.slice(0, 300);
    out.title = title;
    if (typeof spec.title === "string") spec.title = title;
  }
  const width = clampInt(instant.width, 80, 2000);
  const height = clampInt(instant.height, 60, 2000);
  if (width === null && height === null) return out;
  if (plot.kind !== "vega-lite") return out;
  const isConcat = ["hconcat", "vconcat", "concat"].some((k) => k in spec);
  if (isConcat) return out;
  const inner = spec.spec;
  const target: Record<string, unknown> = typeof inner === "object" && inner !== null && !Array.isArray(inner) ? { ...(inner as Record<string, unknown>) } : spec;
  if (width !== null) target.width = width;
  if (height !== null) target.height = height;
  if (target !== spec) spec.spec = target;
  return out;
}

/** The operator turned live previews off (`409 PREVIEW_DISABLED`): offer "Apply" instead. */
export function isPreviewDisabled(e: unknown): boolean {
  return e instanceof ApiError && e.code === "PREVIEW_DISABLED";
}

/** The per-minute or per-day preview limit was reached (`429`; `retryAfter` says when to try again). */
export function isPreviewRateLimited(e: unknown): e is ApiError {
  return e instanceof ApiError && e.status === 429;
}

export type PreviewPhase = "idle" | "sending" | "drawing" | "done" | "failed";

export interface PreviewOptions {
  signal?: AbortSignal | undefined;
  /** Told when the request moves on (`sending` → `drawing` while a run works → the end). */
  onPhase?: ((phase: PreviewPhase) => void) | undefined;
  /** For tests: the poll period and the give-up time. */
  pollMs?: number;
  giveUpMs?: number;
}

function abortError(signal: AbortSignal): Error {
  return signal.reason instanceof Error ? signal.reason : new DOMException("aborted", "AbortError");
}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(abortError(signal));
    const t = setTimeout(resolve, ms);
    signal?.addEventListener(
      "abort",
      () => {
        clearTimeout(t);
        reject(abortError(signal));
      },
      { once: true },
    );
  });
}

/** Read a preview run's result once. */
export async function fetchPreviewResult(s: ProjectScope, runId: string, signal?: AbortSignal): Promise<RecipePreviewResult> {
  return (await unwrap(
    api.GET("/v1/catalog/plots/previews/{run_id}", { params: { path: { run_id: runId } }, headers: s.headers, ...(signal ? { signal } : {}) }),
  ));
}

/**
 * Ask for a preview of recipe `plotId@version` with `settings` (only the settings that need
 * a run are sent) and wait for it: the server holds the request up to {@link PREVIEW_WAIT_S}
 * seconds, then this function polls the run. Resolves with the final result (`done: true`
 * with a plot, or a `problem`); throws `ApiError` for a refusal (invalid settings → 422
 * `SCHEMA_INVALID` with pointers, 409 `PREVIEW_DISABLED`, 429).
 */
export async function requestRecipePreview(
  s: ProjectScope,
  plotId: string,
  version: string,
  settings: Settings,
  opts: PreviewOptions = {},
): Promise<RecipePreviewResult> {
  const { signal, onPhase } = opts;
  const pollMs = opts.pollMs ?? PREVIEW_POLL_MS;
  const giveUp = Date.now() + (opts.giveUpMs ?? PREVIEW_GIVE_UP_MS);
  onPhase?.("sending");
  try {
    const { data } = await unwrapWithStatus(
      api.POST("/v1/catalog/plots/{plot_id}/{version}:preview", {
        params: { path: { plot_id: plotId, version } },
        headers: { ...s.headers, Prefer: `wait=${PREVIEW_WAIT_S}` },
        body: { settings: splitSettings(settings).server },
        ...(signal ? { signal } : {}),
      }),
    );
    let result = data as RecipePreviewResult;
    while (!result.done && !result.problem) {
      if (!result.run_id) throw new ApiError(502, null, "the preview has no run to wait for");
      if (Date.now() > giveUp) throw new ApiError(504, null, "the preview took too long; try again");
      onPhase?.("drawing");
      await sleep(pollMs, signal);
      result = await fetchPreviewResult(s, result.run_id, signal);
    }
    onPhase?.(result.plot ? "done" : "failed");
    if (result.plot) emitOnboarding("recipe_tried", { recipe: `${plotId}@${version}` });
    return result;
  } catch (e) {
    onPhase?.("failed");
    throw e;
  }
}

export interface RecipePreviewState {
  /** The last plot drawn for these settings (the previous one stays while a new one is drawn). */
  plot: PlotSpec | null;
  phase: PreviewPhase;
  /** The run's problem (the recipe could not draw with these settings). */
  problem: Problem | null;
  /** The request was refused (invalid settings, limits, previews off). */
  error: unknown;
  cached: boolean;
  /** Previews are off on this server: show "Apply" (a full render) instead. */
  disabled: boolean;
}

/**
 * The live preview of a recipe for `settings` (debounce them first: {@link PREVIEW_DEBOUNCE_MS}).
 * Only the settings that need a run are part of the key, so editing the title or the size
 * sends nothing; apply those with {@link applyInstantSettings} on the returned plot.
 */
export function useRecipePreview(ref: { plotId: string; version: string } | null, settings: Settings, opts: { enabled?: boolean } = {}): RecipePreviewState {
  const s = useProjectScope();
  const [phase, setPhase] = useState<PreviewPhase>("idle");
  const key = serverSettingsKey(settings);
  const q = useQuery({
    queryKey: ["p", s.key, "plot-preview", ref?.plotId ?? "", ref?.version ?? "", key] as const,
    queryFn: ({ signal }) => requestRecipePreview(s, ref?.plotId ?? "", ref?.version ?? "", settings, { signal, onPhase: setPhase }),
    enabled: (opts.enabled ?? true) && ref !== null && s.key !== "-",
    placeholderData: (prev) => prev,
    staleTime: Infinity,
    gcTime: 10 * 60_000,
    retry: false,
  });
  const data = q.data;
  return {
    plot: data?.plot ?? null,
    phase: q.isFetching ? (phase === "idle" || phase === "done" ? "sending" : phase) : q.isError ? "failed" : data ? (data.plot ? "done" : "failed") : "idle",
    problem: (data?.problem) ?? null,
    error: q.error,
    cached: Boolean(data?.cached),
    disabled: isPreviewDisabled(q.error),
  };
}
