/**
 * Several model versions side by side (M3.5): `POST /v1/models:compare` explores the first
 * version with the others to compare and writes the comparison view (holdout metrics, a
 * ranking per metric and pairwise Diebold-Mariano tests), and every version's own fit
 * diagnostics are read next to it.
 */
import { useQueries, useQuery } from "@tanstack/react-query";
import { Plus, Trophy, X } from "lucide-react";
import { useId, useMemo, useState } from "react";

import { useModels, useModelVersions, useProjectScope, modelArtifactLink } from "@/api/queries";
import type { Model } from "@/api/types";
import { ErrorNotice, LoadingRows, Mono } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Badge, NativeSelect } from "@/components/ui/primitives";
import { fetchArtifactJson } from "@/lib/artifacts";
import { cn } from "@/lib/utils";

import { exploreKeys, type Exploration } from "./api";
import { comparisonReading, dmPair } from "./plain";
import { num, pValue, PlainSummary, SectionCard, Statistics, StatTable, TestDecision, ViewNotes } from "./ui";
import { parseView, viewName, type ComparisonView, type ModelDiagnosticsView } from "./views";

/** `model://project/name@v3` → `{ name, version }`. */
export function parseModelRef(ref: string): { project: string; name: string; version: number } | null {
  const m = /^model:\/\/([^/]+)\/([^@\s]+)@v(\d+)$/.exec(ref);
  return m ? { project: m[1]!, name: m[2]!, version: Number(m[3]) } : null;
}

const LOWER_IS_BETTER = new Set(["rmse", "mae", "mape", "smape", "wape", "mase", "crps"]);

/** The best value of each metric among the versions (lower for errors, higher otherwise). */
export function bestByMetric(view: Pick<ComparisonView, "table" | "metrics">): Record<string, string | null> {
  const out: Record<string, string | null> = {};
  for (const metric of view.metrics ?? []) {
    let best: [string, number] | null = null;
    for (const [ref, row] of Object.entries(view.table ?? {})) {
      const v = row[metric];
      if (v === null || v === undefined || metric === "n_params") continue;
      const better = best === null || (LOWER_IS_BETTER.has(metric) ? v < best[1] : metric.startsWith("coverage") ? Math.abs(v - 0.9) < Math.abs(best[1] - 0.9) : v > best[1]);
      if (better) best = [ref, v];
    }
    out[metric] = best?.[0] ?? null;
  }
  return out;
}

// ------------------------------------------------------------------------------ picker

function VersionAdder({ models, onAdd, exclude }: { models: Model[]; onAdd: (ref: string) => void; exclude: readonly string[] }) {
  const [modelId, setModelId] = useState(models[0]?.id ?? "");
  const versions = useModelVersions(modelId || undefined);
  const options = (versions.data?.items ?? []).filter((v) => !exclude.includes(v.ref)).sort((a, b) => b.version - a.version);
  const [ref, setRef] = useState("");
  const chosen = options.find((v) => v.ref === ref)?.ref ?? options[0]?.ref ?? "";
  const id = useId();
  return (
    <div className="flex flex-wrap items-end gap-2">
      <div className="grid gap-1">
        <label htmlFor={`${id}-model`} className="text-xs font-medium text-muted-foreground">
          Model
        </label>
        <NativeSelect id={`${id}-model`} value={modelId} onChange={(e) => setModelId(e.target.value)} className="h-8 w-56 text-[13px]">
          {models.map((m) => (
            <option key={m.id} value={m.id}>
              {m.name}
            </option>
          ))}
        </NativeSelect>
      </div>
      <div className="grid gap-1">
        <label htmlFor={`${id}-version`} className="text-xs font-medium text-muted-foreground">
          Version
        </label>
        <NativeSelect id={`${id}-version`} value={chosen} onChange={(e) => setRef(e.target.value)} className="h-8 w-28 text-[13px]" disabled={!options.length}>
          {options.map((v) => (
            <option key={v.ref} value={v.ref}>
              v{v.version}
            </option>
          ))}
        </NativeSelect>
      </div>
      <Button size="sm" variant="outline" disabled={!chosen} onClick={() => chosen && onAdd(chosen)}>
        <Plus /> Add
      </Button>
    </div>
  );
}

export function ComparePicker({ refs, onChange }: { refs: string[]; onChange: (refs: string[]) => void }) {
  const models = useModels();
  if (models.isPending) return <LoadingRows rows={2} />;
  if (models.isError) return <ErrorNotice error={models.error} title="The models could not be loaded" />;
  return (
    <div className="grid gap-3">
      <ol className="flex flex-wrap gap-2" aria-label="Versions to compare">
        {refs.map((r, i) => (
          <li key={r} className="inline-flex items-center gap-1.5 rounded-full border bg-card py-0.5 pr-1 pl-3 text-[13px]">
            {i === 0 ? <Badge variant="info">first</Badge> : null}
            <Mono>{r}</Mono>
            <Button variant="ghost" size="icon-sm" aria-label={`Remove ${r}`} onClick={() => onChange(refs.filter((x) => x !== r))}>
              <X />
            </Button>
          </li>
        ))}
        {!refs.length ? <li className="text-[13px] text-muted-foreground">Add two or more versions.</li> : null}
      </ol>
      {refs.length < 11 ? <VersionAdder models={models.data.items} exclude={refs} onAdd={(r) => onChange([...refs, r])} /> : null}
      <p className="text-xs text-muted-foreground">The tests compare forecasts on the first version&apos;s held-out periods, so put the version you care about first.</p>
    </div>
  );
}

// ------------------------------------------------------------------------------ results

function useComparisonView(exploration: Exploration | undefined) {
  const s = useProjectScope();
  const link = exploration?.views?.find((l) => l.name === viewName("comparison"));
  return useQuery({
    queryKey: ["p", s.key, "explorations", exploration?.run.id ?? "", "comparison"],
    enabled: Boolean(link),
    staleTime: 10 * 60_000,
    queryFn: async ({ signal }) => parseView("comparison", await fetchArtifactJson(link!, { signal })),
  });
}

/** Each version's fit-time diagnostics (AIC/BIC, R-hat, convergence) from its own explorer views. */
function useDiagnostics(refs: readonly string[]) {
  const s = useProjectScope();
  const models = useModels();
  const byName = useMemo(() => new Map((models.data?.items ?? []).map((m) => [m.name, m.id])), [models.data]);
  return useQueries({
    queries: refs.map((ref) => {
      const parsed = parseModelRef(ref);
      const id = parsed ? byName.get(parsed.name) : undefined;
      return {
        queryKey: [...exploreKeys.view(s.key, { kind: "model" as const, id: id ?? "", version: parsed?.version ?? 0 }, "base", "diagnostics")],
        enabled: Boolean(id && parsed),
        staleTime: 10 * 60_000,
        retry: false,
        queryFn: async ({ signal }: { signal: AbortSignal }): Promise<ModelDiagnosticsView | null> => {
          try {
            const link = await modelArtifactLink(id!, parsed!.version, viewName("diagnostics"));
            return parseView("diagnostics", await fetchArtifactJson(link, { signal }));
          } catch {
            return null;
          }
        },
      };
    }),
  });
}

export function ComparisonResult({ exploration, refs, expert }: { exploration: Exploration; refs: readonly string[]; expert: boolean }) {
  const view = useComparisonView(exploration);
  const diags = useDiagnostics(refs);
  if (!exploration.done) return null;
  if (view.isError) return <ErrorNotice error={view.error} title="The comparison could not be loaded" />;
  if (!view.data) {
    const skipped = exploration.index?.sections?.find((s) => s.section === "comparison");
    return skipped && skipped.status !== "ok" ? <ErrorNotice error={new Error(skipped.message_plain ?? "The comparison could not be computed.")} /> : <LoadingRows rows={4} />;
  }
  const v = view.data;
  const reading = comparisonReading(v);
  const best = bestByMetric(v);
  const metrics = v.metrics ?? [];
  const statKeys = [...new Set(diags.flatMap((d) => Object.keys(d.data?.stats ?? {})))].filter((k) => /aic|bic|r_hat|ess|diverg|converged|llf/.test(k));
  return (
    <div className="grid gap-5">
      <SectionCard title="Which version forecasts best?" description="Accuracy on held-out data, side by side.">
        <PlainSummary tone={reading.tone}>{reading.text}</PlainSummary>
        <StatTable
          label="Holdout metrics"
          rows={v.models}
          rowKey={(r) => r}
          columns={[
            { key: "model", header: "Version", cell: (r) => <Mono className={cn(Object.values(best).includes(r) && "font-semibold")}>{r}</Mono> },
            ...metrics.map((m) => ({
              key: m,
              header: <span title={LOWER_IS_BETTER.has(m) ? "lower is better" : undefined}>{m.toUpperCase()}</span>,
              numeric: true,
              cell: (r: string) => (
                <span className={cn("inline-flex items-center gap-1", best[m] === r && "font-semibold text-success")}>
                  {best[m] === r ? <Trophy aria-label="best" className="size-3" /> : null}
                  {num(v.table?.[r]?.[m] ?? null)}
                </span>
              ),
            })),
          ]}
        />
        <ViewNotes caption={v.caption_plain} warnings={v.warnings_plain} />
      </SectionCard>
      <SectionCard title="Is the difference more than chance?" description="Diebold-Mariano tests on the shared held-out periods (with the small-sample correction).">
        <StatTable
          label="Diebold-Mariano tests"
          rows={v.tests ?? []}
          rowKey={(t, i) => `${t.var ?? ""}-${i}`}
          empty="No shared held-out periods: the versions need a time holdout for these tests."
          columns={[
            { key: "pair", header: "Pair", cell: (t) => <span className="font-mono text-xs">{dmPair(t).join(" vs ") || t.var}</span> },
            { key: "verdict", header: "In plain words", cell: (t) => t.verdict_plain },
            ...(expert
              ? [
                  { key: "stat", header: "Statistic", numeric: true, cell: (t: NonNullable<ComparisonView["tests"]>[number]) => num(t.statistic, 3) },
                  { key: "p", header: "p-value", numeric: true, cell: (t: NonNullable<ComparisonView["tests"]>[number]) => pValue(t.p_value) },
                  { key: "d", header: "Decision", cell: (t: NonNullable<ComparisonView["tests"]>[number]) => <TestDecision test={t} /> },
                  { key: "n", header: "Periods", numeric: true, cell: (t: NonNullable<ComparisonView["tests"]>[number]) => num(t.n_obs ?? null) },
                ]
              : []),
          ]}
        />
      </SectionCard>
      <SectionCard title="Fit diagnostics side by side" description="Each version's own fit statistics (compare information criteria only between models fitted on the same data).">
        <Statistics expert={expert} label="Show the diagnostics">
          <StatTable
            label="Diagnostics by version"
            rows={refs.map((r, i) => ({ ref: r, view: diags[i]?.data ?? null, loading: diags[i]?.isPending ?? false }))}
            rowKey={(r) => r.ref}
            columns={[
              { key: "ref", header: "Version", cell: (r) => <Mono>{r.ref}</Mono> },
              { key: "inf", header: "Inference", cell: (r) => (r.loading ? "…" : (r.view?.inference ?? "—")) },
              ...statKeys.map((k) => ({ key: k, header: k, numeric: true, cell: (r: { view: ModelDiagnosticsView | null }) => num(r.view?.stats?.[k] ?? null) })),
            ]}
          />
          {expert && Object.keys(v.ranking ?? {}).length ? (
            <StatTable
              label="Ranking per metric"
              rows={Object.entries(v.ranking ?? {})}
              rowKey={([m]) => m}
              columns={[
                { key: "m", header: "Metric", cell: ([m]) => m.toUpperCase() },
                { key: "r", header: "Best first", cell: ([, rs]) => <span className="font-mono text-xs">{rs.join("  >  ")}</span> },
              ]}
            />
          ) : null}
        </Statistics>
      </SectionCard>
    </div>
  );
}
