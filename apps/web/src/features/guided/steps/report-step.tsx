/**
 * Step 5, "Results": the headline numbers (return on spend and share of results per channel,
 * each with its 90% range; how well the model fits) and the sanity scorecard, grouped by
 * question, each check with what it means and what to do.
 */
import { useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { ArrowRight, Microscope, RefreshCw } from "lucide-react";
import { useState } from "react";
import { stringify } from "yaml";

import { useModelPlots, type ModelPlot } from "../model-plots";
import { can } from "@/api/access";
import { api, errorMessage, unwrap } from "@/api/client";
import { qk, usePlot, useProjectScope } from "@/api/queries";
import type { ModelVersion, Scorecard } from "@/api/types";
import { EmptyState, ErrorNotice, LoadingRows, Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle, Skeleton } from "@/components/ui/primitives";
import { useGrants } from "@/features/auth/workspace";
import { PlotFrame } from "@/features/plots/plot-frame";
import { useModelPlotData } from "@/features/plots/plot-data";
import { cn } from "@/lib/utils";

import { useResponseGrid } from "../api";
import { StepActions } from "../frame";
import { amount, holdoutFraction, ratio, share } from "../plain";
import { useGuidedLock } from "../persona";
import { canFitMoreCarefully, withCarefulFit } from "../refit";
import { GUIDED_MESSAGE, exploreTitle } from "../session";
import { StatusBadge, Figure, type CheckStatus } from "../ui";
import type { GuidedSession } from "../use-session";
import { baselineShare, channelHeadlines, type ChannelHeadline, type ResponseGrid } from "../whatif";

const CATEGORIES: { id: string; title: string; question: string }[] = [
  { id: "convergence", title: "Did the model settle?", question: "Whether the estimates are stable enough to read." },
  { id: "holdout", title: "Does it forecast well?", question: "How it did on weeks it was not shown." },
  { id: "plausibility", title: "Are the numbers believable?", question: "Returns and shares that would be surprising." },
  { id: "data", title: "Is the data good enough?", question: "Gaps, history and variation in your data." },
];

const OVERALL: Record<CheckStatus, { title: string; body: string }> = {
  pass: { title: "All checks passed", body: "You can rely on these numbers and share them." },
  warn: { title: "Mostly fine, with something to look at", body: "Read the checks marked 'Check' before you act on the numbers." },
  fail: { title: "Some checks failed", body: "Fix what the failed checks point to before you use these numbers." },
};

/**
 * The convergence checks' remedy as one button: Guided screens hide the fit settings, so
 * "run more tuning and draws" or "raise target_accept" would otherwise be a dead end.
 */
function FitMoreCarefully({ session, failed }: { session: GuidedSession; failed: boolean }) {
  const scope = useProjectScope();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const grants = useGrants();
  const lock = useGuidedLock();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fit = (session.document.model as Record<string, unknown> | undefined)?.fit;
  if (!canFitMoreCarefully(fit)) return null;
  const allowed = can(grants, "update", "spec") && !lock.locked;
  const refit = async () => {
    setBusy(true);
    setError(null);
    try {
      const doc = withCarefulFit(session.document as Record<string, unknown>);
      await unwrap(
        api.POST("/v1/specs/{spec_id}/revisions", {
          params: { path: { spec_id: session.spec.id } },
          body: { yaml: stringify(doc), parent_revision: session.latest.revision, message: `${GUIDED_MESSAGE} fit again, more carefully` },
          headers: scope.headers,
        }),
      );
      await Promise.all([qc.invalidateQueries({ queryKey: qk.spec(scope.key, session.spec.id) }), qc.invalidateQueries({ queryKey: qk.specRevisions(scope.key, session.spec.id) })]);
      void navigate({ to: "/guided/$specId/$step", params: { specId: session.spec.id, step: "run" } });
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="grid gap-2 rounded-lg border p-4" data-testid="fit-more-carefully">
      <p className="font-medium">{failed ? "The model has not settled yet" : "The model could settle further"}</p>
      <p className="text-[13px] text-muted-foreground">
        Fitting again with longer, more careful sampling usually fixes the checks under &ldquo;Did the model settle?&rdquo;. Your data and beliefs stay as they are; the fit takes about twice as long.
      </p>
      <div className="flex flex-wrap items-center gap-3">
        <Button onClick={() => void refit()} disabled={!allowed || busy}>
          {busy ? <Spinner /> : <RefreshCw />} Fit again, more carefully
        </Button>
        {!allowed ? <span className="text-xs text-muted-foreground">Ask a project admin to fit it again.</span> : null}
      </div>
      {error ? (
        <p role="alert" className="text-[13px] text-destructive">
          {error}
        </p>
      ) : null}
    </div>
  );
}

function ScorecardView({ scorecard, mv, session }: { scorecard: Scorecard; mv: ModelVersion; session: GuidedSession }) {
  const items = scorecard.items ?? [];
  const known = new Set(CATEGORIES.map((c) => c.id));
  const groups = [
    ...CATEGORIES.map((c) => ({ ...c, items: items.filter((i) => i.category === c.id) })),
    { id: "other", title: "Other checks", question: "", items: items.filter((i) => !i.category || !known.has(i.category)) },
  ].filter((g) => g.items.length);
  const counts = { pass: 0, warn: 0, fail: 0 };
  for (const i of items) counts[i.status] += 1;
  return (
    <section aria-labelledby="scorecard-h" className="grid gap-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="grid gap-1">
          <h2 id="scorecard-h" className="text-[17px] font-semibold tracking-tight">
            Sanity scorecard
          </h2>
          <p className="text-[13px] text-muted-foreground">
            {counts.pass} passed · {counts.warn} to check · {counts.fail} failed
          </p>
        </div>
        <Button asChild variant="outline" size="sm">
          <Link to="/models/$modelId/versions/$version" params={{ modelId: mv.model_id, version: String(mv.version) }} search={{ tab: "diagnostics" }}>
            <Microscope /> Inspect in Expert mode
          </Link>
        </Button>
      </div>
      <div className={cn("flex items-start gap-3 rounded-lg border p-4", scorecard.overall === "pass" ? "border-success/30 bg-success/6" : scorecard.overall === "warn" ? "border-warning/35 bg-warning/8" : "border-destructive/30 bg-destructive/6")}>
        <StatusBadge status={scorecard.overall} />
        <div>
          <p className="font-medium">{OVERALL[scorecard.overall].title}</p>
          <p className="text-[13px] text-muted-foreground">{OVERALL[scorecard.overall].body}</p>
        </div>
      </div>
      {items.some((i) => i.category === "convergence" && i.status !== "pass") ? (
        <FitMoreCarefully session={session} failed={items.some((i) => i.category === "convergence" && i.status === "fail")} />
      ) : null}
      <div className="grid gap-4 lg:grid-cols-2">
        {groups.map((g) => (
          <Card key={g.id}>
            <CardHeader className="pb-2">
              <div className="grid gap-0.5">
                <CardTitle>{g.title}</CardTitle>
                {g.question ? <CardDescription>{g.question}</CardDescription> : null}
              </div>
            </CardHeader>
            <CardContent>
              <ul className="grid divide-y">
                {g.items.map((item) => (
                  <li key={item.check ?? item.code} className="grid gap-1.5 py-3 first:pt-0 last:pb-0" data-check={item.check ?? item.code}>
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <p className="font-medium">{item.title}</p>
                      <StatusBadge status={item.status} />
                    </div>
                    <p className="text-[13px] text-muted-foreground">{item.message_plain}</p>
                    {item.status !== "pass" && item.suggested_action ? (
                      <p className="text-[13px]">
                        <span className="font-medium">What to do: </span>
                        {item.suggested_action}
                      </p>
                    ) : null}
                  </li>
                ))}
              </ul>
            </CardContent>
          </Card>
        ))}
      </div>
      {Object.keys(scorecard.skipped ?? {}).length ? (
        <details className="rounded-lg border px-4 py-3 text-[13px]">
          <summary className="cursor-pointer font-medium">Checks that could not run ({Object.keys(scorecard.skipped ?? {}).length})</summary>
          <ul className="grid gap-1 pt-2 text-muted-foreground">
            {Object.entries(scorecard.skipped ?? {}).map(([k, why]) => (
              <li key={k}>
                <span className="font-mono text-[12px]">{k}</span>: {why}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </section>
  );
}

function IntervalCell({ lo, mid, hi, max, fmt }: { lo: number; mid: number; hi: number; max: number; fmt: (v: number) => string }) {
  const x = (v: number) => `${Math.max(0, Math.min(100, (v / (max || 1)) * 100))}%`;
  return (
    <div className="grid gap-1">
      <p className="tabular">
        <span className="font-semibold">{fmt(mid)}</span> <span className="text-xs text-muted-foreground">({fmt(lo)} to {fmt(hi)})</span>
      </p>
      <div aria-hidden className="relative h-1.5 rounded-full bg-muted">
        <div className="absolute inset-y-0 rounded-full bg-primary/35" style={{ left: x(lo), width: `calc(${x(hi)} - ${x(lo)})` }} />
        <div className="absolute -top-0.5 h-2.5 w-1 -translate-x-1/2 rounded-full bg-primary" style={{ left: x(mid) }} />
      </div>
    </div>
  );
}

function ChannelTable({ rows, grid }: { rows: ChannelHeadline[]; grid: ResponseGrid }) {
  const maxRoi = Math.max(...rows.map((r) => r.roi?.quantiles[2] ?? 0));
  const maxShare = Math.max(...rows.map((r) => r.share?.quantiles[2] ?? 0));
  const units = grid.spend_units;
  return (
    <div className="overflow-x-auto rounded-lg border [contain:inline-size]">
      <table className="w-full min-w-[34rem] text-[13px]">
        <caption className="sr-only">Return on spend and share of results per channel, with 90% ranges</caption>
        <thead>
          <tr className="border-b text-xs text-muted-foreground">
            <th scope="col" className="px-3 py-2 text-left font-medium">
              Channel
            </th>
            <th scope="col" className="px-3 py-2 text-left font-medium">
              Spent
            </th>
            <th scope="col" className="px-3 py-2 text-left font-medium">
              Return per 1 spent
            </th>
            <th scope="col" className="px-3 py-2 text-left font-medium">
              Share of results
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.channel} className="border-b last:border-0 align-top">
              <th scope="row" className="px-3 py-2.5 text-left font-semibold">
                {r.channel}
              </th>
              <td className="px-3 py-2.5 tabular">{amount(r.spend, units)}</td>
              <td className="w-[32%] px-3 py-2.5">{r.roi ? <IntervalCell lo={r.roi.quantiles[0]!} mid={r.roi.mean} hi={r.roi.quantiles[2]!} max={maxRoi} fmt={ratio} /> : "—"}</td>
              <td className="w-[32%] px-3 py-2.5">{r.share ? <IntervalCell lo={r.share.quantiles[0]!} mid={r.share.mean} hi={r.share.quantiles[2]!} max={maxShare} fmt={(v) => share(v, 1)} /> : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ModelPlotFigure({ mv, plot }: { mv: ModelVersion; plot: ModelPlot }) {
  const spec = usePlot(mv.model_id, mv.version, plot.id);
  const data = useModelPlotData(mv.model_id, mv.version, spec.data);
  if (spec.isError || data.isError) return <ErrorNotice error={spec.error ?? data.error} title={`"${plot.title}" could not be loaded`} />;
  if (!data.data) return <Skeleton className="h-64" />;
  return (
    <figure className="grid gap-1.5">
      <figcaption className="text-[13px] font-medium">{plot.title}</figcaption>
      <PlotFrame kind={data.data.spec.kind} spec={data.data.body} datasets={data.data.datasets} title={plot.title} height={280} exportName={plot.id} />
      {plot.caption_plain ? <p className="text-xs text-muted-foreground">{plot.caption_plain}</p> : null}
    </figure>
  );
}

export function ReportStep({ session }: { session: GuidedSession }) {
  const navigate = useNavigate();
  const mv = session.modelVersion;
  const grid = useResponseGrid(mv);
  const plots = useModelPlots(mv);
  if (!session.lastGoodFit) {
    return (
      <EmptyState title="No results yet" action={<Button onClick={() => void navigate({ to: "/guided/$specId/$step", params: { specId: session.spec.id, step: "run" } })}>Fit the model</Button>}>
        Results appear here once the model has been fitted.
      </EmptyState>
    );
  }
  if (!mv) return <LoadingRows rows={5} />;
  const rows = grid.data ? channelHeadlines(grid.data) : [];
  const base = grid.data ? baselineShare(grid.data) : null;
  const best = [...rows].sort((a, b) => (b.roi?.mean ?? -Infinity) - (a.roi?.mean ?? -Infinity))[0];
  const mape = holdoutFraction(mv, "mape");
  const coverage = holdoutFraction(mv, "coverage_90") ?? holdoutFraction(mv, "coverage_80");
  const shown = (plots.data ?? []).filter((p) => p.id === "core.ppc_fan" || p.id === "core.fit_vs_actual" || p.id === "mmm.response_curves").slice(0, 2);
  const stale = session.lastGoodFit.spec_revision !== session.latest.revision;
  return (
    <div className="grid gap-8">
      {stale ? <p className="rounded-lg border border-info/35 bg-info/8 px-4 py-2 text-[13px]">These results are from before your latest changes. Fit again to update them.</p> : null}
      <section aria-labelledby="headline-h" className="grid gap-4">
        <h2 id="headline-h" className="sr-only">
          Headline numbers
        </h2>
        <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
          {base ? <Figure label="Results driven by media" value={share(1 - base.mean)} detail={`between ${share(1 - base.quantiles[2]!)} and ${share(1 - base.quantiles[0]!)}`} /> : null}
          {best?.roi ? <Figure label="Best return on spend" value={best.channel} detail={`${ratio(best.roi.mean)} back per 1 spent`} /> : null}
          {mape != null ? <Figure label="Forecast error on held-back weeks" value={share(mape)} detail="on average, lower is better" /> : null}
          {coverage != null ? <Figure label="Weeks inside the 90% range" value={share(coverage)} detail="close to 90% means the ranges are honest" /> : null}
        </div>
        {grid.isPending ? <Skeleton className="h-40" /> : rows.length ? <ChannelTable rows={rows} grid={grid.data!} /> : null}
        {rows.length ? <p className="text-xs text-muted-foreground">Each number is the model's best estimate; the range in brackets is where the true value lies with 90% probability.</p> : null}
      </section>
      {shown.length ? (
        <section aria-label="Charts" className="grid gap-6 xl:grid-cols-2">
          {shown.map((p) => (
            <ModelPlotFigure key={p.id} mv={mv} plot={p} />
          ))}
        </section>
      ) : null}
      {mv.scorecard ? <ScorecardView scorecard={mv.scorecard} mv={mv} session={session} /> : <p className="text-[13px] text-muted-foreground">This model has no scorecard.</p>}
      <StepActions>
        <Button variant="ghost" onClick={() => void navigate({ to: "/guided/$specId/$step", params: { specId: session.spec.id, step: "run" } })}>
          Back
        </Button>
        <Button onClick={() => void navigate({ to: "/guided/$specId/$step", params: { specId: session.spec.id, step: "explore" } })}>
          {exploreTitle(session.template)} <ArrowRight />
        </Button>
      </StepActions>
    </div>
  );
}
