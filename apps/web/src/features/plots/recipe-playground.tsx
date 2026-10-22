/**
 * The recipe playground (v0.18 D21 (ii)): "Try it" on a plot card. The recipe's example chart
 * next to a form built from its `settings_schema`:
 *
 * - **title, width and height** change in the browser at once (no request);
 * - **every other setting** is debounced ({@link PREVIEW_DEBOUNCE_MS}) and sent as one
 *   `POST /v1/catalog/plots/{id}/{version}:preview`. A fresh draw is a real RENDER run, about
 *   10 s on a laptop (WP0 probe P11), so the page shows a pending state for the whole wait and
 *   never promises "instant"; repeats come from the server's 7-day cache;
 * - a refused setting (`422`, issue pointers `/settings/<key>`) shows under its field;
 * - when the operator turned live previews off (`409 PREVIEW_DISABLED`) the playground stops
 *   asking on every change and offers **Apply** instead;
 * - "Copy as YAML" gives the snippets that put the chart in a spec (`outputs.plots`) or draw it
 *   with these settings in a workflow (`plot.render`).
 *
 * The plot is drawn in the plot sandbox like every other plot. Lazy chunk
 * (`recipe-playground-*`); signed-in surfaces only (never in share or client chunks).
 */
import { useQueryClient } from "@tanstack/react-query";
import { Check, Copy, Play } from "lucide-react";
import { useEffect, useId, useMemo, useState } from "react";

import { ApiError } from "@/api/client";
import type { PlotCatalogEntry, PlotSpec } from "@/api/types";
import { Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/overlays";
import { type Json, type JsonObject, SchemaForm, type SchemaNode } from "@/features/forms/json-schema-form";
import { plainIssueMessage } from "@/lib/plain-issue";
import { parsePointer } from "@/lib/pointer";
import { cn } from "@/lib/utils";

import { resolvePlotDatasets, type ResolvedPlot } from "./plot-data";
import { usePlotExample } from "./plot-example";
import { PlotFrame } from "./plot-frame";
import {
  applyInstantSettings,
  INSTANT_SETTINGS,
  isPreviewRateLimited,
  PREVIEW_DEBOUNCE_MS,
  type RecipePreviewState,
  serverSettingsKey,
  splitSettings,
  useRecipePreview,
} from "./preview-api";

// ----------------------------------------------------------------------------- pure helpers

const INSTANT: ReadonlySet<string> = new Set(INSTANT_SETTINGS);

function obj(v: Json | undefined): SchemaNode | null {
  return v && typeof v === "object" && !Array.isArray(v) ? v : null;
}

/** `schema` with only the properties `keep` says yes to (same `required`, same rest). */
export function pickProperties(schema: SchemaNode, keep: (key: string) => boolean): SchemaNode {
  const props = obj(schema.properties) ?? {};
  const out: SchemaNode = { ...schema, properties: Object.fromEntries(Object.entries(props).filter(([k]) => keep(k))) };
  if (Array.isArray(schema.required)) out.required = schema.required.filter((k) => typeof k === "string" && keep(k));
  return out;
}

/** Field key → message from a refused preview (`422` issues at `/settings/<key>` or `/<key>`). */
export function settingErrors(error: unknown): Record<string, string> {
  if (!(error instanceof ApiError) || !error.problem) return {};
  const out: Record<string, string> = {};
  for (const issue of error.problem.errors ?? []) {
    let segs: string[];
    try {
      segs = parsePointer(issue.pointer ?? "");
    } catch {
      continue;
    }
    if (segs[0] === "settings") segs = segs.slice(1);
    const key = segs[0];
    if (key && !(key in out)) out[key] = plainIssueMessage(issue.message);
  }
  return out;
}

/**
 * The variables each core chart's synthetic example can draw (its `var` / `vars` setting), so
 * the playground can say which names work here (v0.18 live: the field was free text and a
 * guess like `y` only earned a refusal ten seconds later). Read off `mapi_plots.examples`:
 * every name of the example data that renders when passed as the setting. A chart missing
 * here (a plugin's) gets no hint.
 */
export const EXAMPLE_VARIABLES: Readonly<Record<string, readonly string[]>> = {
  "core.data_overview": ["revenue", "spend"],
  "core.prior_predictive": ["revenue"],
  "core.ppc": ["revenue"],
  "core.ppc_fan": ["revenue"],
  "core.fit_vs_actual": ["revenue"],
  "core.residuals_over_time": ["revenue"],
  "core.decomposition": ["baseline", "channel_contribution"],
  "core.model_comparison": ["revenue"],
  "core.counterfactual": ["revenue"],
  "core.decision_distribution": ["objective"],
  "core.ridge": ["effect"],
  "core.rank_matrix": ["effect"],
  "core.calibration": ["y"],
  "core.state_sequence": ["state_prob"],
  "core.roc_pr": ["p"],
  "core.confusion": ["p"],
  "core.loss_curve": ["loss"],
  "core.loo_pit": ["y"],
};

/** The settings that name variables of the data. */
const VARIABLE_KEYS = ["var", "vars"] as const;

/**
 * `schema` with a hint on its variable settings that names the example's variables, and the
 * names to offer there (`suggest` of {@link SchemaForm}). Unchanged without known variables.
 */
export function withExampleVariables(schema: SchemaNode, plotId: string): { schema: SchemaNode; suggest: Record<string, string[]> } {
  const names = EXAMPLE_VARIABLES[plotId];
  const props = obj(schema.properties);
  if (!names?.length || !props) return { schema, suggest: {} };
  const list = names.length === 1 ? names[0]! : `${names.slice(0, -1).join(", ")} or ${names.at(-1)!}`;
  const suggest: Record<string, string[]> = {};
  const next: Record<string, Json> = { ...props };
  for (const key of VARIABLE_KEYS) {
    const prop = obj(props[key]);
    if (!prop) continue;
    suggest[key] = [...names];
    const own = typeof prop.description === "string" && prop.description.trim() ? `${prop.description.trim().replace(/\.?$/, ".")} ` : "";
    const empty = key === "vars" ? "Leave it empty for all of them." : "Leave it empty for the model's main one.";
    next[key] = { ...prop, description: `${own}The example has ${list}. ${empty}` };
  }
  return { schema: { ...schema, properties: next }, suggest };
}

const PLAIN = /^[A-Za-z_][A-Za-z0-9_.-]*$/;
const RESERVED = /^(?:true|false|yes|no|on|off|y|n|null|~)$/i;

/** One YAML scalar or flow collection (JSON is valid YAML flow; plain words stay plain). */
function yamlValue(v: Json): string {
  if (typeof v === "string") return PLAIN.test(v) && !RESERVED.test(v) ? v : JSON.stringify(v);
  return JSON.stringify(v);
}

/** The node id a workflow would give the chart: the recipe name (`core.ppc_fan` → `ppc_fan`). */
function nodeId(recipeId: string): string {
  const name = recipeId.split(".").pop() ?? "chart";
  return /^[a-z][a-z0-9_]*$/.test(name) ? name : "chart";
}

/**
 * The snippets that use this chart: the spec's `outputs.plots` (default settings; a spec lists
 * refs only) and a workflow `plot.render` node with these settings.
 */
export function recipeYaml(recipeId: string, version: string, settings: JsonObject): string {
  const major = version.split(".")[0] || "1";
  const ref = `${recipeId}@^${major}`;
  const lines = [
    "# In a model spec: draw this chart after every fit (it uses its default settings).",
    "outputs:",
    "  plots:",
    `    - ${ref}`,
    "",
    "# In a workflow: draw it from a fitted model with your settings.",
    "nodes:",
    `  - id: ${nodeId(recipeId)}`,
    "    use: plot.render",
    "    inputs: {model: fit.model}",
    "    with:",
    `      recipe: ${ref}`,
  ];
  const keys = Object.keys(settings)
    .filter((k) => settings[k] !== undefined)
    .sort();
  if (keys.length) {
    lines.push("      settings:");
    for (const k of keys) lines.push(`        ${PLAIN.test(k) ? k : JSON.stringify(k)}: ${yamlValue(settings[k] as Json)}`);
  }
  return `${lines.join("\n")}\n`;
}

/** `value`, held back until it has been still for `ms` (compared by `key`). */
function useDebounced<T>(value: T, key: string, ms: number): T {
  const [held, setHeld] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setHeld(value), ms);
    return () => clearTimeout(t);
    // `key` stands for `value`: a new object with the same content does not restart the wait.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, ms]);
  return held;
}

type StatusTone = "idle" | "busy" | "ok" | "warn" | "error";

interface Status {
  tone: StatusTone;
  text: string;
}

function previewStatus(opts: {
  hasServer: boolean;
  waiting: boolean;
  applyMode: boolean;
  dirtyApply: boolean;
  preview: RecipePreviewState;
  fieldErrors: number;
  drawFailed: boolean;
}): Status {
  const { hasServer, waiting, applyMode, dirtyApply, preview, fieldErrors, drawFailed } = opts;
  if (applyMode && (preview.disabled || !hasServer)) {
    return dirtyApply
      ? { tone: "warn", text: "Live previews are off on this server. Click Apply to draw the chart with your settings." }
      : preview.disabled
        ? { tone: "warn", text: "Live previews are off on this server. Your settings are in the YAML below. They apply when the chart is drawn for a model." }
        : { tone: "idle", text: "This is the example chart. Title and size change at once; click Apply for the other settings." };
  }
  if (waiting || (hasServer && (preview.phase === "sending" || preview.phase === "drawing"))) {
    return { tone: "busy", text: "Redrawing with your settings. A new chart takes about 10 seconds; settings you tried before come back quickly." };
  }
  if (!hasServer) return { tone: "idle", text: "This is the example chart. Title and size change at once; other settings redraw it on the server." };
  if (fieldErrors) return { tone: "error", text: "A setting has a problem. Correct the field that shows the message." };
  if (isPreviewRateLimited(preview.error)) {
    const wait = preview.error.retryAfter;
    return { tone: "error", text: `Too many previews in a short time. Wait ${wait ? `${wait} seconds` : "a minute"}, then change a setting again.` };
  }
  if (preview.error) {
    return { tone: "error", text: preview.error instanceof Error ? `The preview did not work: ${preview.error.message}` : "The preview did not work. Try again." };
  }
  if (drawFailed) return { tone: "error", text: "The app cannot show the preview. Change a setting to try again." };
  if (preview.problem) return { tone: "error", text: `The chart cannot draw with these settings: ${preview.problem.detail ?? preview.problem.title}` };
  if (preview.phase === "done") return { tone: "ok", text: preview.cached ? "Drawn with your settings (from the cache)." : "Drawn with your settings." };
  return { tone: "idle", text: "Change a setting to redraw the chart." };
}

const STATUS_CLASS: Record<StatusTone, string> = {
  idle: "bg-muted text-muted-foreground",
  busy: "bg-accent text-accent-foreground",
  ok: "bg-highlight-soft text-highlight-ink",
  warn: "bg-attention-soft text-attention",
  error: "bg-destructive/12 text-destructive",
};

// ----------------------------------------------------------------------------- the playground

/** The preview drawn for `plot` with its datasets decoded (`failed` when they cannot be). */
function useResolvedPreview(plot: PlotSpec | null): { resolved: ResolvedPlot | null; failed: boolean } {
  const [resolved, setResolved] = useState<ResolvedPlot | null>(null);
  const [failed, setFailed] = useState<PlotSpec | null>(null);
  useEffect(() => {
    if (!plot) return;
    const ctl = new AbortController();
    resolvePlotDatasets(plot, () => Promise.reject(new Error("a preview must embed its datasets")), ctl.signal)
      .then((datasets) => {
        if (!ctl.signal.aborted) setResolved({ spec: plot, body: plot.spec as ResolvedPlot["body"], datasets });
      })
      .catch(() => {
        if (!ctl.signal.aborted) setFailed(plot);
      });
    return () => ctl.abort();
  }, [plot]);
  return { resolved: plot ? resolved : null, failed: plot !== null && failed === plot };
}

function CopyYaml({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      className="rounded-full"
      onClick={() => {
        void navigator.clipboard?.writeText(text).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        });
      }}
    >
      {copied ? <Check /> : <Copy />} {copied ? "Copied" : "Copy as YAML"}
    </Button>
  );
}

/** The playground of one catalog plot (use {@link RecipePlaygroundDialog} for "Try it"). */
export function RecipePlayground({ plot, className }: { plot: PlotCatalogEntry; className?: string }) {
  const version = plot.version ?? "1.0.0";
  const title = plot.about?.title ?? plot.title;
  const qc = useQueryClient();
  const formId = useId();
  const schema = useMemo(() => (plot.settings_schema ? (plot.settings_schema as SchemaNode) : null), [plot.settings_schema]);
  const quickSchema = useMemo(() => (schema ? pickProperties(schema, (k) => INSTANT.has(k)) : null), [schema]);
  const more = useMemo(() => (schema ? withExampleVariables(pickProperties(schema, (k) => !INSTANT.has(k)), plot.id) : null), [schema, plot.id]);
  const moreSchema = more?.schema ?? null;
  const quickCount = Object.keys(obj(quickSchema?.properties) ?? {}).length;
  const moreCount = Object.keys(obj(moreSchema?.properties) ?? {}).length;

  const [settings, setSettings] = useState<JsonObject>({});
  const server = useMemo(() => splitSettings(settings).server as JsonObject, [settings]);
  const serverKey = serverSettingsKey(settings);
  const debounced = useDebounced(server, serverKey, PREVIEW_DEBOUNCE_MS);
  const [applyMode, setApplyMode] = useState(false);
  const [applied, setApplied] = useState<JsonObject>({});
  const requested = applyMode ? applied : debounced;
  const requestedKey = serverSettingsKey(requested);
  const hasServer = requestedKey !== "[]";
  const preview = useRecipePreview(hasServer ? { plotId: plot.id, version } : null, requested);
  useEffect(() => {
    if (preview.disabled) setApplyMode(true);
  }, [preview.disabled]);

  const example = usePlotExample(plot.id, version);
  const { resolved: drawnPreview, failed: drawFailed } = useResolvedPreview(hasServer ? preview.plot : null);
  const base: ResolvedPlot | null = hasServer && drawnPreview ? drawnPreview : (example.data ?? null);
  const instantKey = JSON.stringify(splitSettings(settings).instant);
  const shown = useMemo(() => {
    if (!base) return null;
    const spec = applyInstantSettings(base.spec, settings);
    return { kind: spec.kind, body: spec.spec as ResolvedPlot["body"], datasets: base.datasets };
    // Only the instant settings change the drawn spec here; the rest arrive as a new `base`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [base, instantKey]);

  const fieldErrors = useMemo(() => (hasServer && requestedKey === serverKey ? settingErrors(preview.error) : {}), [hasServer, requestedKey, serverKey, preview.error]);
  const status = previewStatus({
    hasServer,
    // Debouncing, or the drawn chart is still being decoded.
    waiting: (!applyMode && serverKey !== requestedKey) || (hasServer && preview.plot !== null && !drawFailed && drawnPreview?.spec !== preview.plot),
    applyMode,
    dirtyApply: applyMode && serverKey !== "[]" && serverKey !== requestedKey,
    preview,
    fieldErrors: Object.keys(fieldErrors).length,
    drawFailed,
  });
  const yaml = useMemo(() => recipeYaml(plot.id, version, settings), [plot.id, version, settings]);

  const apply = () => {
    setApplied(server);
    // Ask again even when these exact settings were refused before (previews may be back on).
    void qc.resetQueries({ predicate: (q) => q.queryKey[2] === "plot-preview" && q.queryKey[3] === plot.id });
  };

  return (
    <div data-testid="recipe-playground" data-plot={plot.id} className={cn("grid gap-6 lg:grid-cols-[minmax(0,19rem)_minmax(0,1fr)]", className)}>
      <form
        id={formId}
        aria-label={`Settings of ${title}`}
        data-tour="catalog.recipe.settings"
        className="grid content-start gap-5"
        onSubmit={(e) => {
          e.preventDefault();
          if (applyMode) apply();
        }}
      >
        {!schema ? (
          <p className="text-sm text-muted-foreground">This chart has no settings to play with. Its example is on the right.</p>
        ) : null}
        {schema && quickCount ? (
          <fieldset className="grid gap-3">
            <legend className="mb-2 flex flex-wrap items-center gap-2 text-sm font-semibold">
              Look
              <span className="rounded-full bg-highlight-soft px-2 py-0.5 text-xs font-semibold text-highlight-ink">changes at once</span>
            </legend>
            <SchemaForm schema={quickSchema as SchemaNode} value={settings} skip={Object.keys(obj(moreSchema?.properties) ?? {})} onChange={setSettings} empty="" />
          </fieldset>
        ) : null}
        {schema && moreCount ? (
          <fieldset className="grid gap-3">
            <legend className="mb-2 flex flex-wrap items-center gap-2 text-sm font-semibold">
              Chart settings
              <span className="rounded-full bg-muted px-2 py-0.5 text-xs font-semibold text-muted-foreground">redraws in about 10 s</span>
            </legend>
            <SchemaForm schema={moreSchema as SchemaNode} value={settings} skip={[...INSTANT]} errors={fieldErrors} suggest={more?.suggest ?? {}} onChange={setSettings} empty="" />
          </fieldset>
        ) : null}
        {applyMode ? (
          <Button type="submit" className="justify-self-start rounded-full" disabled={serverKey === "[]"}>
            <Play /> Apply
          </Button>
        ) : null}
      </form>

      <div className="grid min-w-0 content-start gap-3">
        <p
          data-testid="recipe-preview-status"
          data-tone={status.tone}
          role="status"
          aria-live="polite"
          className={cn("flex items-start gap-2 rounded-xl px-3 py-2 text-xs font-medium", STATUS_CLASS[status.tone])}
        >
          {status.tone === "busy" ? <Spinner className="mt-px shrink-0" /> : null}
          <span>{status.text}</span>
        </p>
        <div className={cn("relative rounded-2xl border bg-card p-2 transition-opacity", status.tone === "busy" && "opacity-70")}>
          {shown ? (
            <PlotFrame kind={shown.kind} spec={shown.body} datasets={shown.datasets} title={`Preview: ${title}`} height={320} exportName={`${plot.id}-preview`} />
          ) : example.data === null || example.isError ? (
            <p className="p-6 text-center text-sm text-muted-foreground">{example.isError ? "The app cannot load the example." : "This chart has no example to draw."}</p>
          ) : (
            <div aria-hidden className="h-80 w-full rounded-lg bg-muted/60" />
          )}
        </div>
        <p className="text-xs text-hint">An example made from synthetic data. On your model the chart uses your data.</p>
        <details data-tour="catalog.recipe.yaml" className="group rounded-xl border bg-card">
          <summary className="flex cursor-pointer flex-wrap items-center justify-between gap-2 px-4 py-2.5 text-sm font-semibold">
            Use it in a spec or a workflow
            <span className="text-xs font-medium text-muted-foreground group-open:hidden">Show the YAML</span>
          </summary>
          <div className="grid gap-2 px-4 pb-4">
            <pre data-testid="recipe-yaml" className="max-h-64 overflow-auto rounded-lg bg-muted p-3 font-mono text-xs leading-relaxed whitespace-pre">
              {yaml}
            </pre>
            <div className="flex justify-end">
              <CopyYaml text={yaml} />
            </div>
          </div>
        </details>
      </div>
    </div>
  );
}

/** "Try it": the recipe playground in a wide dialog (lazy-load this module from the plot card). */
export function RecipePlaygroundDialog({ plot, open, onOpenChange }: { plot: PlotCatalogEntry; open: boolean; onOpenChange: (open: boolean) => void }) {
  const title = plot.about?.title ?? plot.title;
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent wide className="top-[6vh] max-h-[88vh] max-w-5xl">
        <div className="grid gap-1 pr-8">
          <p className="eyebrow">Try it</p>
          <DialogTitle className="text-2xl font-bold">{title}</DialogTitle>
          <DialogDescription>
            Play with the settings and watch the example chart change. When it looks right, copy the YAML into your spec or workflow.
          </DialogDescription>
        </div>
        {open ? <RecipePlayground plot={plot} /> : null}
      </DialogContent>
    </Dialog>
  );
}

export default RecipePlaygroundDialog;
