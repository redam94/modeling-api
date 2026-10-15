/**
 * Usage and cost (`/v1/usage`, `/v1/tenants/{t}/usage`): ledger totals for one UTC day or
 * month, broken down by day, project, token and resource class, plus the quota limits in
 * force with 70 % / 90 % warnings.
 */
import { getRouteApi, Link, useNavigate } from "@tanstack/react-router";
import { AlertTriangle, ChevronLeft, ChevronRight, Cpu, Info, Receipt, Rows3, Server } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";

import { can } from "@/api/access";
import { useTenantUsage, useTokens, useUsage, type UsageQuery } from "@/api/queries";
import type { CostTotals, LimitName, QuotaLimitStatus, UsageReport } from "@/api/types";
import { BarChart, BarList, Meter, meterTone, type BarDatum } from "@/components/charts";
import { ErrorNotice, LoadingRows, Mono, PageHeader } from "@/components/common";
import { Button } from "@/components/ui/button";
import { SegmentedControl } from "@/components/ui/overlays";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/primitives";
import { useWorkspace } from "@/features/auth/workspace";
import { compactNumber, formatMicros, formatVcpuSeconds } from "@/lib/cost";
import { formatBytes } from "@/lib/format";
import { humanWait } from "@/lib/problems";
import { cn } from "@/lib/utils";

const route = getRouteApi("/usage");

// ----------------------------------------------------------------------- periods

/** `"2026-10"` / `"2026-10-15"` for the period containing `date` (UTC). */
export function periodKey(period: "day" | "month", date: Date): string {
  const iso = date.toISOString();
  return period === "month" ? iso.slice(0, 7) : iso.slice(0, 10);
}

/** The RFC 3339 instant the API takes as `start` (the first instant of the period). */
export function periodStart(period: "day" | "month", key: string): string {
  return period === "month" ? `${key.slice(0, 7)}-01T00:00:00Z` : `${key.slice(0, 10)}T00:00:00Z`;
}

/** The previous (`-1`) or next (`+1`) period key. */
export function shiftPeriod(period: "day" | "month", key: string, delta: number): string {
  const d = new Date(periodStart(period, key));
  if (period === "month") d.setUTCMonth(d.getUTCMonth() + delta);
  else d.setUTCDate(d.getUTCDate() + delta);
  return periodKey(period, d);
}

export function periodLabel(period: "day" | "month", key: string): string {
  const d = new Date(periodStart(period, key));
  return d.toLocaleDateString("en", period === "month" ? { month: "long", year: "numeric", timeZone: "UTC" } : { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
}

/** One bar per day of the report window (days without spend are zero). */
export function dailySeries(report: UsageReport, metric: "cost" | "compute"): BarDatum[] {
  const out: BarDatum[] = [];
  const end = Date.parse(report.end);
  for (let t = Date.parse(report.start); t < end && out.length < 62; t += 86_400_000) {
    const day = new Date(t).toISOString().slice(0, 10);
    const g = report.groups?.[day];
    out.push({
      key: day,
      label: String(Number(day.slice(8, 10))),
      title: new Date(t).toLocaleDateString("en", { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" }),
      value: metric === "cost" ? (g?.amount_micros ?? 0) : (g?.vcpu_seconds ?? 0),
    });
  }
  return out;
}

// ----------------------------------------------------------------------- quotas

export const LIMIT_LABEL: Record<LimitName, string> = {
  requests_per_minute: "Requests per minute",
  max_resource_class: "Largest resource class",
  max_fanout_tasks: "Tasks per run",
  concurrent_runs: "Concurrent runs",
  vcpu_seconds_per_day: "Compute today",
  vcpu_seconds_per_month: "Compute this month",
  storage_bytes: "Storage",
  egress_bytes_per_month: "Downloads this month",
  untrusted_execution: "Uploaded code",
  global_spend: "Platform spend",
};

export function formatLimitValue(name: LimitName, v: number | null | undefined): string {
  if (v == null) return "—";
  if (name.startsWith("vcpu_seconds")) return formatVcpuSeconds(v);
  if (name === "storage_bytes" || name === "egress_bytes_per_month") return formatBytes(v);
  if (name === "global_spend") return formatMicros(v);
  return v.toLocaleString("en");
}

const SCOPE_LABEL = { token: "token", tenant: "organisation", global: "platform" } as const;

function QuotaRow({ q }: { q: QuotaLimitStatus }) {
  const fraction = q.fraction_used ?? (q.limit ? q.used / q.limit : null);
  const tone = meterTone(fraction);
  const label = LIMIT_LABEL[q.limit_name] ?? q.limit_name;
  const value = `${formatLimitValue(q.limit_name, q.used)} of ${formatLimitValue(q.limit_name, q.limit)}`;
  const resetIn = q.resets_at ? (Date.parse(q.resets_at) - Date.now()) / 1000 : null;
  return (
    <li className="grid gap-1.5">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 text-[13px]">
        <span className="flex items-center gap-1.5 font-medium">
          {tone !== "ok" ? <AlertTriangle className={cn("size-3.5", tone === "critical" ? "text-destructive" : "text-warning")} aria-hidden /> : null}
          {label}
          <span className="font-normal text-muted-foreground">· {SCOPE_LABEL[q.scope]}</span>
        </span>
        <span className="tabular text-muted-foreground">
          {value}
          {fraction != null ? <span className={cn("ml-2 font-medium", tone === "critical" ? "text-destructive" : tone === "warn" ? "text-warning" : "text-foreground")}>{Math.round(fraction * 100)}%</span> : null}
        </span>
      </div>
      <Meter label={`${label} (${SCOPE_LABEL[q.scope]})`} fraction={fraction} valueText={`${value}${fraction != null ? `, ${Math.round(fraction * 100)} percent` : ""}`} />
      {tone !== "ok" || resetIn ? (
        <p className="text-xs text-muted-foreground">
          {tone === "critical" ? "Almost used up: new work may be refused. " : tone === "warn" ? "Over 70% used. " : ""}
          {resetIn && resetIn > 0 ? `Resets in ${humanWait(resetIn)}.` : ""}
        </p>
      ) : null}
    </li>
  );
}

function QuotaCard({ quotas }: { quotas: QuotaLimitStatus[] }) {
  const metered = quotas.filter((q) => q.limit != null).sort((a, b) => (b.fraction_used ?? 0) - (a.fraction_used ?? 0));
  const near = metered.filter((q) => meterTone(q.fraction_used) !== "ok").length;
  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle>Quotas</CardTitle>
          <CardDescription>
            {near ? `${near} limit${near === 1 ? " is" : "s are"} above 70%.` : "Every limit has room. Warnings start at 70% and 90%."}
          </CardDescription>
        </div>
      </CardHeader>
      <CardContent>
        {metered.length ? (
          <ul className="grid gap-4" aria-label="Quota limits">
            {metered.map((q) => (
              <QuotaRow key={`${q.scope}:${q.subject_id}:${q.limit_name}`} q={q} />
            ))}
          </ul>
        ) : (
          <p className="text-muted-foreground">No quota limits apply.</p>
        )}
      </CardContent>
    </Card>
  );
}

// ----------------------------------------------------------------------- page

function Kpi({ icon, label, value, sub }: { icon: ReactNode; label: string; value: string; sub?: ReactNode }) {
  return (
    <div className="grid gap-1 rounded-lg border bg-card p-4">
      <span className="flex items-center gap-2 text-xs text-muted-foreground [&_svg]:size-3.5">
        {icon}
        {label}
      </span>
      <span className="text-[22px] leading-tight font-semibold tracking-tight tabular">{value}</span>
      {sub ? <span className="text-xs text-muted-foreground">{sub}</span> : null}
    </div>
  );
}

function totalsKpis(t: CostTotals, currency: string | null | undefined): ReactNode {
  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      <Kpi icon={<Receipt />} label="Cost" value={formatMicros(t.amount_micros ?? 0, t.currency ?? currency ?? "USD")} />
      <Kpi icon={<Cpu />} label="Compute" value={formatVcpuSeconds(t.vcpu_seconds ?? 0)} />
      <Kpi icon={<Rows3 />} label="Runs" value={(t.runs ?? 0).toLocaleString("en")} sub={t.attempts && t.attempts !== t.runs ? `${t.attempts} attempts billed` : undefined} />
      <Kpi icon={<Server />} label="Memory" value={`${compactNumber((t.gib_seconds ?? 0) / 3600)} GiB·h`} />
    </div>
  );
}

export function UsagePage() {
  const ws = useWorkspace();
  const search = route.useSearch();
  const navigate = useNavigate({ from: "/usage" });
  const period = search.period ?? "month";
  const key = search.start ?? periodKey(period, new Date());
  const tenantScope = search.scope === "tenant" && can(ws.grants, "read", "billing");
  const canTenant = can(ws.grants, "read", "billing");
  const [metric, setMetric] = useState<"cost" | "compute">("cost");
  const tenantId = ws.current?.tenant.id ?? "";
  const start = periodStart(period, key);
  const isCurrent = key === periodKey(period, new Date());

  const q = (group_by?: UsageQuery["group_by"]): UsageQuery => ({ period, start, ...(group_by ? { group_by } : {}) });
  const seriesGroup: UsageQuery["group_by"] = period === "month" ? "day" : "resource_class";
  const projectSeries = useUsage(q(seriesGroup), !tenantScope);
  const projectClasses = useUsage(q("resource_class"), !tenantScope);
  const projectTokens = useUsage(q("token"), !tenantScope);
  const tenantSeries = useTenantUsage(tenantId, q(seriesGroup), tenantScope);
  const tenantProjects = useTenantUsage(tenantId, q("project"), tenantScope);
  const tenantClasses = useTenantUsage(tenantId, q("resource_class"), tenantScope);
  const tokens = useTokens(can(ws.grants, "manage", "token") ? { all: true } : {});

  const series = tenantScope ? tenantSeries : projectSeries;
  const classes = tenantScope ? tenantClasses : projectClasses;
  const breakdown = tenantScope ? tenantProjects : projectTokens;
  const report = series.data;
  const currency = report?.totals.currency ?? "USD";
  const fmt = metric === "cost" ? (v: number) => formatMicros(v, currency) : formatVcpuSeconds;
  const axis = metric === "cost" ? (v: number) => formatMicros(v, currency).replace(/\.0+(?=\D*$)/, "") : (v: number) => compactNumber(v);

  const tokenNames = useMemo(() => new Map((tokens.data?.items ?? []).map((t) => [t.id, t.name])), [tokens.data]);
  const projectNames = useMemo(() => new Map(ws.choices.map((c) => [c.project.project_id, c.project.name])), [ws.choices]);

  const go = (patch: Partial<{ period: "day" | "month"; start: string | undefined; scope: "tenant" | undefined }>) =>
    void navigate({ search: (s) => ({ ...s, ...patch }) });

  return (
    <>
      <PageHeader
        title="Usage and cost"
        description={
          <>
            Compute and spend of <span className="font-medium text-foreground">{tenantScope ? ws.current?.tenant.name : ws.current?.project.name}</span> in UTC {period === "month" ? "months" : "days"}.
            {report?.price_versions?.length ? <> Priced with <Mono>{report.price_versions.join(", ")}</Mono>.</> : null}
          </>
        }
        actions={
          <>
            {canTenant ? (
              <SegmentedControl<"project" | "tenant">
                label="Usage scope"
                value={tenantScope ? "tenant" : "project"}
                onValueChange={(v) => go({ scope: v === "tenant" ? "tenant" : undefined })}
                options={[
                  { value: "project", label: "Project" },
                  { value: "tenant", label: "Organisation" },
                ]}
              />
            ) : null}
            <SegmentedControl<"day" | "month">
              label="Period"
              value={period}
              onValueChange={(p) => go({ period: p === "month" ? undefined : p, start: undefined })}
              options={[
                { value: "day", label: "Day" },
                { value: "month", label: "Month" },
              ]}
            />
            <div className="flex items-center gap-1" role="group" aria-label="Choose the period">
              <Button variant="outline" size="icon-sm" aria-label="Previous period" onClick={() => go({ start: shiftPeriod(period, key, -1) })}>
                <ChevronLeft />
              </Button>
              <span className="min-w-28 text-center text-[13px] font-medium tabular" aria-live="polite">
                {periodLabel(period, key)}
              </span>
              <Button variant="outline" size="icon-sm" aria-label="Next period" disabled={isCurrent} onClick={() => go({ start: shiftPeriod(period, key, 1) })}>
                <ChevronRight />
              </Button>
            </div>
          </>
        }
      />
      {series.isPending ? (
        <LoadingRows rows={5} />
      ) : series.isError ? (
        <ErrorNotice error={series.error} title="Usage could not be loaded" onRetry={() => void series.refetch()} />
      ) : report ? (
        <div className="grid gap-5">
          {totalsKpis(report.totals, currency)}
          <div className="grid gap-5 xl:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
            <Card>
              <CardHeader>
                <div>
                  <CardTitle>{period === "month" ? `Daily ${metric === "cost" ? "cost" : "compute"}` : `${metric === "cost" ? "Cost" : "Compute"} by resource class`}</CardTitle>
                  <CardDescription>{periodLabel(period, key)}</CardDescription>
                </div>
                <SegmentedControl<"cost" | "compute">
                  label="Metric"
                  value={metric}
                  onValueChange={setMetric}
                  options={[
                    { value: "cost", label: "Cost" },
                    { value: "compute", label: "vCPU·s" },
                  ]}
                />
              </CardHeader>
              <CardContent>
                {period === "month" ? (
                  <BarChart label={`Daily ${metric} in ${periodLabel(period, key)}`} data={dailySeries(report, metric)} format={fmt} axisFormat={axis} />
                ) : (
                  <BarChart
                    label={`${metric} by resource class on ${periodLabel(period, key)}`}
                    data={Object.entries(report.groups ?? {}).map(([k, g]) => ({ key: k, label: k, title: `Class ${k}`, value: metric === "cost" ? (g.amount_micros ?? 0) : (g.vcpu_seconds ?? 0) }))}
                    format={fmt}
                    axisFormat={axis}
                  />
                )}
              </CardContent>
            </Card>
            <QuotaCard quotas={report.quotas ?? []} />
          </div>
          <div className="grid gap-5 lg:grid-cols-2">
            <Card>
              <CardHeader>
                <div>
                  <CardTitle>{tenantScope ? "By project" : "By token"}</CardTitle>
                  <CardDescription>{tenantScope ? "Every project in the organisation." : "Runs started with an API token; browser and CLI sessions are listed as “Signed-in users”."}</CardDescription>
                </div>
                {!tenantScope ? (
                  <Link to="/tokens" className="shrink-0 text-xs whitespace-nowrap text-muted-foreground hover:text-foreground">
                    Manage tokens
                  </Link>
                ) : null}
              </CardHeader>
              <CardContent>
                <BreakdownList
                  query={breakdown}
                  metric={metric}
                  currency={currency}
                  label={tenantScope ? "Cost by project" : "Cost by token"}
                  name={(k) => (tenantScope ? (projectNames.get(k) ?? k) : k === "" || k === "none" || k === "session" ? "Signed-in users" : (tokenNames.get(k) ?? k))}
                />
              </CardContent>
            </Card>
            <Card>
              <CardHeader>
                <div>
                  <CardTitle>By resource class</CardTitle>
                  <CardDescription>S 2 vCPU · M 4 vCPU · L 8 vCPU</CardDescription>
                </div>
              </CardHeader>
              <CardContent>
                <BreakdownList query={classes} metric={metric} currency={currency} label="Cost by resource class" name={(k) => `Class ${k}`} />
              </CardContent>
            </Card>
          </div>
          <p className="flex items-start gap-2 text-xs text-muted-foreground">
            <Info className="mt-0.5 size-3.5 shrink-0" />
            Amounts come from the platform&apos;s cost ledger: metered vCPU- and GiB-seconds at list prices. They are an estimate of compute cost, not an invoice.
          </p>
        </div>
      ) : null}
    </>
  );
}

function BreakdownList({
  query,
  metric,
  currency,
  label,
  name,
}: {
  query: { data?: UsageReport | undefined; isPending: boolean; isError: boolean; error: unknown };
  metric: "cost" | "compute";
  currency: string;
  label: string;
  name: (key: string) => string;
}) {
  if (query.isPending) return <LoadingRows rows={3} />;
  if (query.isError) return <ErrorNotice error={query.error} />;
  const groups = Object.entries(query.data?.groups ?? {});
  return (
    <BarList
      label={label}
      format={metric === "cost" ? (v) => formatMicros(v, currency) : formatVcpuSeconds}
      items={groups.map(([k, g]) => ({
        key: k,
        label: name(k),
        value: metric === "cost" ? (g.amount_micros ?? 0) : (g.vcpu_seconds ?? 0),
        sub: `${(g.runs ?? 0).toLocaleString("en")} run${g.runs === 1 ? "" : "s"} · ${metric === "cost" ? formatVcpuSeconds(g.vcpu_seconds ?? 0) : formatMicros(g.amount_micros ?? 0, currency)}`,
      }))}
    />
  );
}
