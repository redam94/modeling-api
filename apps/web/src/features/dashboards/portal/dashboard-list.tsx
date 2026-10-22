/**
 * The client portal's home (`/c`, MEI home): the dashboards shared with the signed-in client in
 * this project, as cards with the latest readout and when it was updated.
 */
import { ArrowRight } from "lucide-react";

import { InlineMessage, TagWord } from "@/components/ods";
import { Skeleton } from "@/components/ui/primitives";
import { useWorkspace } from "@/features/auth/workspace";

import { useDashboards } from "../api";
import { dashboardErrorMessage } from "../client";
import { localTitle, stamp } from "../readout/readout-model";
import type { DashboardSummary } from "../types";

const PERIOD_WORD: Record<NonNullable<DashboardSummary["period_label"]>, string> = {
  week: "Weekly",
  month: "Monthly",
  period: "Each period",
  day: "Daily",
  quarter: "Quarterly",
  year: "Yearly",
  case: "Per case",
};

function DashboardCard({ d, onOpen }: { d: DashboardSummary; onOpen: (id: string) => void }) {
  const latest = d.latest_readout ?? null;
  return (
    <li className="min-w-0">
      <a
        href={`/c/${encodeURIComponent(d.id)}`}
        onClick={(e) => {
          if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
          e.preventDefault();
          onOpen(d.id);
        }}
        className="focus-ring group grid h-full gap-3 rounded-xl border bg-card p-5 transition-colors hover:border-primary hover:bg-surface-hover max-sm:p-4"
        data-dashboard={d.id}
      >
        <div className="flex items-start justify-between gap-3">
          <div className="grid min-w-0 gap-1">
            {d.client_name ? <span className="eyebrow">{d.client_name}</span> : null}
            <span className="text-lg font-semibold text-foreground">{d.title}</span>
          </div>
          <ArrowRight aria-hidden className="mt-1 size-4 flex-none text-hint group-hover:text-primary" />
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <TagWord tone="neutral">{PERIOD_WORD[d.period_label ?? "week"]}</TagWord>
          {d.live_inputs === "paused" ? <TagWord tone="warning">New results paused</TagWord> : null}
        </div>
        <p className="text-sm text-muted-foreground">
          {latest ? `Latest: ${localTitle(latest)}. Updated ${stamp(latest.ready_at ?? latest.created_at)}.` : "There are no results yet."}
        </p>
      </a>
    </li>
  );
}

export function DashboardList({ onOpen }: { onOpen: (id: string) => void }) {
  const ws = useWorkspace();
  const q = useDashboards({ status: "published" });
  const items = q.data?.items ?? [];
  return (
    <div className="grid gap-6">
      <header className="grid gap-2">
        <h1 className="text-3xl font-semibold tracking-tight text-foreground">Your dashboards</h1>
        <p className="max-w-[68ch] text-base text-muted-foreground">
          The dashboards that your analyst shares with you{ws.current ? ` in ${ws.current.project.name}` : ""}. Choose one to see the newest results.
        </p>
      </header>
      {q.isPending ? (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3" role="status" aria-label="Loading your dashboards">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-36" />
          ))}
        </div>
      ) : q.isError ? (
        <InlineMessage tone="error">{dashboardErrorMessage(q.error)}</InlineMessage>
      ) : items.length ? (
        <ul aria-label="Dashboards" className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {items.map((d) => (
            <DashboardCard key={d.id} d={d} onOpen={onOpen} />
          ))}
        </ul>
      ) : (
        <InlineMessage tone="info">No dashboards are shared with you yet. Your analyst shares them here.</InlineMessage>
      )}
    </div>
  );
}
