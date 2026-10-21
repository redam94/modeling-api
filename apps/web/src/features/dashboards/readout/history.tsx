/**
 * History: every readout the viewer can see, newest first (the published history, plus the
 * viewer's own saved plans and sent numbers). Choose one to read it; "Show older" loads the next
 * page. A sent upload shows whether it waits for review and the state of its refit request.
 */
import { ChevronRight } from "lucide-react";
import { useState } from "react";

import { InlineMessage } from "@/components/ods";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import { dashboardErrorMessage, useReadout, useReadoutList } from "../client";
import type { ClientDashboard, DashboardClient } from "../types";
import { KpiChart, ReadoutState, ReadoutTiles, Section, Tags, Working } from "./parts";
import { historyRow, metaLine, periodOf } from "./readout-model";

const PAGE = 20;

function HistoryPage({
  client,
  beforeSeq,
  selected,
  onSelect,
  onOlder,
  last,
}: {
  client: DashboardClient;
  beforeSeq: number | undefined;
  selected: number | null;
  onSelect: (seq: number) => void;
  onOlder: (before: number) => void;
  last: boolean;
}) {
  const q = useReadoutList(client, { limit: PAGE, ...(beforeSeq !== undefined ? { beforeSeq } : {}) });
  if (q.isPending) return <Working>Loading the readouts…</Working>;
  if (q.isError) return <InlineMessage tone="error">{dashboardErrorMessage(q.error)}</InlineMessage>;
  const items = q.data.items ?? [];
  const next = q.data.next_before_seq ?? null;
  return (
    <>
      {items.map((h) => {
        const row = historyRow(h);
        const active = selected === h.seq;
        return (
          <li key={h.seq}>
            <button
              type="button"
              aria-current={active ? "true" : undefined}
              onClick={() => onSelect(h.seq)}
              className={cn(
                "focus-ring grid w-full grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1 border-b px-3 py-3 text-left hover:bg-surface-hover",
                active && "bg-accent",
              )}
            >
              <span className="grid min-w-0 gap-0.5">
                <span className="truncate text-sm font-semibold text-foreground">{row.title}</span>
                <span className="text-xs text-hint">
                  {row.kind} · {row.when}
                </span>
                <Tags tags={row.tags} className="pt-1" />
              </span>
              <ChevronRight aria-hidden className="size-4 text-hint" />
            </button>
          </li>
        );
      })}
      {last && next !== null ? (
        <li className="px-3 py-3">
          <Button variant="action" size="sm" onClick={() => onOlder(next)}>
            Show older
          </Button>
        </li>
      ) : null}
      {last && !items.length && beforeSeq === undefined ? <li className="px-3 py-3 text-sm text-muted-foreground">There are no readouts yet.</li> : null}
    </>
  );
}

function ReadoutDetail({ client, dashboard, seq }: { client: DashboardClient; dashboard: ClientDashboard; seq: number }) {
  const q = useReadout(client, seq);
  if (q.isPending) return <Working>Loading the readout…</Working>;
  if (q.isError) return <InlineMessage tone="error">{dashboardErrorMessage(q.error)}</InlineMessage>;
  const r = q.data;
  const row = historyRow(r);
  return (
    <Section title={r.title} description={metaLine(r, periodOf(dashboard))} action={<Tags tags={row.tags} />} data-readout="detail">
      <ReadoutState readout={r} />
      {r.status === "ready" && r.snapshot ? (
        <>
          <p className="text-base text-foreground">{r.snapshot.lead}</p>
          <ReadoutTiles dashboard={dashboard} readout={r} label={`Key numbers of ${r.title}`} />
          <KpiChart dashboard={dashboard} readout={r} />
        </>
      ) : null}
    </Section>
  );
}

export function History({ client, dashboard, initial = null }: { client: DashboardClient; dashboard: ClientDashboard; initial?: number | null }) {
  const [cursors, setCursors] = useState<(number | undefined)[]>([undefined]);
  const [selected, setSelected] = useState<number | null>(initial);
  return (
    <div className="grid min-w-0 gap-6 lg:grid-cols-12" data-tab="history">
      <Section className="lg:col-span-5" title="Readouts" description="The newest first. Your saved plans and sent numbers show only to you and the analysts.">
        <ul aria-label="Readouts" className="-mx-5 -mb-5 max-sm:-mx-4 max-sm:-mb-4">
          {cursors.map((c, i) => (
            <HistoryPage
              key={c ?? "first"}
              client={client}
              beforeSeq={c}
              selected={selected}
              onSelect={setSelected}
              last={i === cursors.length - 1}
              onOlder={(before) => setCursors((prev) => [...prev, before])}
            />
          ))}
        </ul>
      </Section>
      <div className="min-w-0 lg:col-span-7">
        {selected !== null ? (
          <ReadoutDetail client={client} dashboard={dashboard} seq={selected} />
        ) : (
          <p className="rounded-xl border border-dashed border-border-strong p-6 text-sm text-muted-foreground">Choose a readout to read it here.</p>
        )}
      </div>
    </div>
  );
}
