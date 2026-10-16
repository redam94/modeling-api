/**
 * A budget plan (`OptimizeResult`): the headline in plain words, today's spend against the
 * recommended one per channel (and market), the expected result with its range, the chance
 * the plan beats today's, and the efficient frontier.
 */
import { AlertTriangle, ArrowDownRight, ArrowUpRight, Info } from "lucide-react";
import { useMemo } from "react";

import { Badge, Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/primitives";
import { allocationPlot, frontierPlot } from "@/features/explore/plots";
import { ExplorerPlot, num, pct, PlainSummary, StatTable } from "@/features/explore/ui";
import { amount, band, signedAmount, signedShare } from "@/features/guided/plain";
import { Figure } from "@/features/guided/ui";
import { cn } from "@/lib/utils";

import type { ChannelAllocation, OptimizeResult } from "./api";

/** The one-paragraph answer: what moves where, what it adds, and how sure that is. */
export function planHeadline(r: OptimizeResult): { text: string; tone: "good" | "neutral" | "warn" } {
  const byChannel = new Map<string, { current: number; optimal: number }>();
  for (const a of r.allocation) {
    const x = byChannel.get(a.channel) ?? { current: 0, optimal: 0 };
    byChannel.set(a.channel, { current: x.current + a.current_spend, optimal: x.optimal + a.optimal_spend });
  }
  const moves = [...byChannel.entries()].map(([ch, v]) => ({ ch, delta: v.optimal - v.current })).sort((a, b) => b.delta - a.delta);
  const up = moves.filter((m) => m.delta > 1e-6 * Math.max(1, r.total_budget));
  const down = moves.filter((m) => m.delta < -1e-6 * Math.max(1, r.total_budget));
  const parts: string[] = [];
  if (r.mode === "free") parts.push(`The model suggests spending ${amount(r.total_budget, r.spend_units)} in all (today ${amount(r.current_total, r.spend_units)}).`);
  else if (Math.abs(r.total_budget - r.current_total) > 1e-6 * Math.max(1, r.current_total)) parts.push(`With ${amount(r.total_budget, r.spend_units)} instead of ${amount(r.current_total, r.spend_units)}:`);
  if (up.length || down.length) {
    const words = (xs: typeof moves) => xs.map((m) => `${m.ch} (${signedAmount(Math.round(m.delta), r.spend_units)})`).join(", ");
    const sentence = down.length && up.length ? `move money from ${words(down)} to ${words(up)}` : down.length ? `spend less on ${words(down)}` : `spend more on ${words(up)}`;
    parts.push(`${parts.length ? sentence : sentence.replace(/^./, (c) => c.toUpperCase())}.`);
  } else parts.push("Today's split is already about the best.");
  let tone: "good" | "neutral" | "warn" = "neutral";
  if (r.uplift) {
    const b = band(r.uplift.quantiles);
    const round = (v: number) => (Math.abs(v) >= 100 ? Math.round(v) : v);
    parts.push(`That adds about ${signedAmount(round(r.uplift.mean), r.kpi_units)} to ${r.kpi}${b ? ` (90% likely between ${signedAmount(round(b[0]), r.kpi_units)} and ${signedAmount(round(b[1]), r.kpi_units)})` : ""}.`);
    if (r.prob_positive_uplift != null) {
      parts.push(`The chance it beats today's plan is ${pct(r.prob_positive_uplift)}.`);
      tone = r.prob_positive_uplift >= 0.8 ? "good" : r.prob_positive_uplift < 0.6 ? "warn" : "neutral";
    }
  }
  return { text: parts.join(" "), tone };
}

function allocationLabel(a: ChannelAllocation): string {
  return a.geo ? `${a.channel} · ${a.geo}` : a.channel;
}

export function PlanResult({ result: r, expert }: { result: OptimizeResult; expert: boolean }) {
  const headline = planHeadline(r);
  const expected = band(r.expected_kpi.quantiles);
  const current = r.current_kpi ? band(r.current_kpi.quantiles) : null;
  const rows = useMemo(() => [...r.allocation].sort((a, b) => b.optimal_spend - a.optimal_spend), [r.allocation]);
  const alloc = useMemo(() => allocationPlot(rows.map((a) => ({ label: allocationLabel(a), current: a.current_spend, optimal: a.optimal_spend })), { units: r.spend_units ?? null }), [rows, r.spend_units]);
  const frontier = useMemo(
    () =>
      (r.frontier ?? []).length
        ? frontierPlot(
            (r.frontier ?? []).map((p) => ({ budget: p.total_budget, kpi: p.expected_kpi, lo: p.kpi_p5 ?? null, hi: p.kpi_p95 ?? null })),
            { current: r.current_total, chosen: r.total_budget },
            { kpi: r.kpi, units: r.spend_units ?? null },
          )
        : null,
    [r],
  );
  const extrapolated = rows.filter((a) => a.extrapolated);
  return (
    <div className="grid gap-5" data-testid="plan-result">
      <PlainSummary tone={headline.tone}>
        <p className="text-[15px]">{headline.text}</p>
        <p className="text-[13px] text-muted-foreground">Goal: {r.objective_label}. Based on {num(r.n_draws)} plausible versions of the model{r.horizon ? `, over ${r.horizon} periods` : ""}.</p>
      </PlainSummary>
      <div className="grid gap-3 sm:grid-cols-3">
        <Figure label={`Expected ${r.kpi} with the plan`} value={amount(r.expected_kpi.mean, r.kpi_units)} detail={expected ? `90% likely ${amount(expected[0], r.kpi_units)} – ${amount(expected[1], r.kpi_units)}` : undefined} />
        <Figure label={`Expected ${r.kpi} today`} value={r.current_kpi ? amount(r.current_kpi.mean, r.kpi_units) : "—"} detail={current ? `90% likely ${amount(current[0], r.kpi_units)} – ${amount(current[1], r.kpi_units)}` : undefined} />
        <Figure
          label="Chance the plan beats today's"
          value={r.prob_positive_uplift != null ? pct(r.prob_positive_uplift) : "—"}
          detail={r.uplift ? `Expected gain ${signedAmount(r.uplift.mean, r.kpi_units)}` : undefined}
        />
      </div>
      <Card>
        <CardHeader>
          <div className="grid gap-1">
            <CardTitle>Where the money goes</CardTitle>
            <CardDescription>Today&apos;s spend against the recommended one{rows.some((a) => a.geo) ? ", per channel and market" : ", per channel"}.</CardDescription>
          </div>
        </CardHeader>
        <CardContent className="grid gap-4">
          <ExplorerPlot plot={alloc} title="Spend today and recommended" height={Math.min(560, 80 + 34 * rows.length)} exportName="budget-allocation" />
          <StatTable
            label="Allocation"
            rows={rows}
            rowKey={(a) => allocationLabel(a)}
            columns={[
              { key: "ch", header: "Channel", cell: (a) => <span className="font-medium">{allocationLabel(a)}</span> },
              { key: "cur", header: "Today", numeric: true, cell: (a) => amount(a.current_spend, r.spend_units) },
              { key: "opt", header: "Recommended", numeric: true, cell: (a) => <span className="font-semibold">{amount(a.optimal_spend, r.spend_units)}</span> },
              {
                key: "chg",
                header: "Change",
                numeric: true,
                cell: (a) =>
                  a.change_pct == null ? (
                    "—"
                  ) : (
                    <span className={cn("inline-flex items-center gap-0.5", a.change_pct > 0 ? "text-success" : a.change_pct < 0 ? "text-destructive" : "")}>
                      {a.change_pct > 0 ? <ArrowUpRight aria-hidden className="size-3.5" /> : a.change_pct < 0 ? <ArrowDownRight aria-hidden className="size-3.5" /> : null}
                      {signedShare(a.change_pct / 100, 0)}
                    </span>
                  ),
              },
              { key: "share", header: "Share", numeric: true, cell: (a) => `${pct(a.current_share)} → ${pct(a.optimal_share)}` },
              ...(expert
                ? [
                    { key: "contrib", header: "Expected contribution", numeric: true, cell: (a: ChannelAllocation) => amount(a.expected_contribution?.mean ?? null, r.kpi_units) },
                    { key: "mroi", header: "Return of the next unit", numeric: true, cell: (a: ChannelAllocation) => num(a.marginal_roi, 3) },
                    { key: "stab", header: "Agreement", numeric: true, cell: (a: ChannelAllocation) => pct(a.stability) },
                  ]
                : []),
              { key: "x", header: "", cell: (a) => (a.extrapolated ? <Badge variant="warning">beyond the data</Badge> : null) },
            ]}
          />
          {extrapolated.length ? (
            <p className="flex items-start gap-1.5 text-[13px]">
              <AlertTriangle aria-hidden className="mt-0.5 size-3.5 shrink-0 text-warning" /> {extrapolated.map(allocationLabel).join(", ")} would spend more than the data has seen; the model is extrapolating there.
            </p>
          ) : null}
          {expert ? <p className="text-xs text-muted-foreground">Agreement: the share of plausible model versions whose own best plan moves the channel the same way.</p> : null}
        </CardContent>
      </Card>
      {frontier ? (
        <Card>
          <CardHeader>
            <div className="grid gap-1">
              <CardTitle>What other budgets would bring</CardTitle>
              <CardDescription>The best expected {r.kpi} at each total budget (the efficient frontier), with its 90% range.</CardDescription>
            </div>
          </CardHeader>
          <CardContent className="grid gap-3">
            <ExplorerPlot plot={frontier} title="Efficient frontier" height={300} exportName="budget-frontier" />
            {expert ? (
              <StatTable
                label="Frontier"
                rows={r.frontier ?? []}
                rowKey={(p) => String(p.total_budget)}
                columns={[
                  { key: "b", header: "Budget", numeric: true, cell: (p) => amount(p.total_budget, r.spend_units) },
                  { key: "k", header: `Expected ${r.kpi}`, numeric: true, cell: (p) => amount(p.expected_kpi, r.kpi_units) },
                  { key: "lo", header: "5%", numeric: true, cell: (p) => amount(p.kpi_p5 ?? null, r.kpi_units) },
                  { key: "hi", header: "95%", numeric: true, cell: (p) => amount(p.kpi_p95 ?? null, r.kpi_units) },
                  { key: "m", header: "Return of the next unit", numeric: true, cell: (p) => num(p.marginal_roi ?? null, 3) },
                ]}
              />
            ) : null}
          </CardContent>
        </Card>
      ) : null}
      {(r.notes_plain ?? []).length || expert ? (
        <ul className="grid gap-1 text-[13px]" aria-label="Notes">
          {(r.notes_plain ?? []).map((n) => (
            <li key={n} className="flex items-start gap-1.5">
              <Info aria-hidden className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" /> {n}
            </li>
          ))}
          {expert ? (
            <li className="text-xs text-muted-foreground">
              Expected regret {num(r.expected_regret ?? null)} · shadow price {num(r.shadow_price ?? null, 3)} · objective {r.objective}
            </li>
          ) : null}
        </ul>
      ) : null}
    </div>
  );
}
