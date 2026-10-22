/**
 * History: every readout the viewer can see, newest first (the published history, plus the
 * viewer's own saved plans and sent numbers). Choose one to read it; "Show older" loads the next
 * page. A sent upload shows whether it waits for review and the state of its refit request.
 */
import { ChevronRight } from "lucide-react";
import { lazy, useEffect, useRef, useState } from "react";

import { InlineMessage } from "@/components/ods";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import { dashboardErrorMessage, useKeepReadout, useReadout, useReadoutList } from "../client";
import { hasPanels, isLayout2Dashboard, type ClientDashboard, type DashboardClient, type DashboardClientV2, type Readout } from "../types";
import { LazyPart } from "./load-boundary";
import { asClientV2 } from "./panel-model";
import { PanelBlocks } from "./panels/blocks";
import { KpiChart, ReadoutState, ReadoutTiles, Section, Tags, Working } from "./parts";
import { historyRow, metaLine, periodOf } from "./readout-model";

const ReadoutResults = lazy(() => import("./panels/readout-extra").then((m) => ({ default: m.ReadoutResults })));

const PAGE = 20;
/** Below `lg` the list and the detail stack: the detail is below the whole list. */
const STACKED = "(max-width: 1023.98px)";

function stacked(): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia(STACKED).matches;
}

function HistoryPage({
  client,
  beforeSeq,
  selected,
  onSelect,
  onOlder,
  last,
  othersWords = false,
}: {
  client: DashboardClient;
  othersWords?: boolean;
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
        const row = historyRow(h, undefined, { othersWords });
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

/** "Keep this case" on the viewer's own private tried case (never on a share link: it has no keep). */
function KeepTried({ client, readout }: { client: DashboardClient; readout: Readout }) {
  const v2 = asClientV2(client);
  const keep = useKeepReadout(v2 ?? (client as DashboardClientV2));
  if (!v2?.keep || readout.kind !== "prediction" || readout.status !== "ready" || readout.mine === false) return null;
  if (readout.visibility !== "private" && readout.visibility !== "pending_review") return null;
  const kept = readout.visibility === "pending_review";
  return (
    <div className="grid gap-1" data-readout="keep">
      <div>
        <Button variant="secondary" size="sm" disabled={kept || keep.isPending} onClick={() => keep.mutate(readout.seq)}>
          Keep this case
        </Button>
      </div>
      <p className="text-xs text-hint">{kept ? "Kept. The analyst checks it before other people see it." : "Only you can see this result. Keep it to show it to the analyst."}</p>
      {keep.isError ? <InlineMessage tone="error">{dashboardErrorMessage(keep.error)}</InlineMessage> : null}
    </div>
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
      {r.status === "ready" && r.snapshot && hasPanels(r.snapshot) && r.snapshot.preset !== "mmm_planner" ? (
        // a layout 2 readout (v0.17): its panels and what its input gave back
        <>
          <p className="text-base text-foreground">{r.snapshot.lead}</p>
          <PanelBlocks client={client} dashboard={dashboard} snapshot={r.snapshot} label={`Key numbers of ${r.title}`} />
          <LazyPart>
            <ReadoutResults client={client} dashboard={dashboard} readout={r} />
          </LazyPart>
          <KeepTried client={client} readout={r} />
        </>
      ) : r.status === "ready" && r.snapshot ? (
        <>
          <p className="text-base text-foreground">{r.snapshot.lead}</p>
          <ReadoutTiles dashboard={dashboard} readout={r} label={`Key numbers of ${r.title}`} />
          <KpiChart dashboard={dashboard} readout={r} />
        </>
      ) : null}
    </Section>
  );
}

const DESCRIPTION = "The newest first. Your saved plans and sent numbers show only to you and the analysts.";

export function History({
  client,
  dashboard,
  initial = null,
  description = DESCRIPTION,
}: {
  client: DashboardClient;
  dashboard: ClientDashboard;
  initial?: number | null;
  /** The words under "Readouts" (layout 2 names its own private readouts). */
  description?: string;
}) {
  const [cursors, setCursors] = useState<(number | undefined)[]>([undefined]);
  const [selected, setSelected] = useState<number | null>(initial);
  const detailRef = useRef<HTMLDivElement>(null);
  const [jump, setJump] = useState(0);
  // On a phone the detail renders below the whole list, off screen: bring it into view and
  // move the focus there (keyboard and screen-reader users hear the readout they chose).
  useEffect(() => {
    if (!jump) return;
    const el = detailRef.current;
    el?.scrollIntoView?.({ block: "start", behavior: "smooth" });
    el?.focus({ preventScroll: true });
  }, [jump]);
  const choose = (seq: number) => {
    setSelected(seq);
    if (stacked()) setJump((j) => j + 1);
  };
  return (
    <div className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-6 lg:grid-cols-12" data-tab="history">
      <Section className="lg:col-span-5" title="Readouts" description={description}>
        <ul aria-label="Readouts" className="-mx-5 -mb-5 max-sm:-mx-4 max-sm:-mb-4">
          {cursors.map((c, i) => (
            <HistoryPage
              key={c ?? "first"}
              client={client}
              othersWords={isLayout2Dashboard(dashboard)}
              beforeSeq={c}
              selected={selected}
              onSelect={choose}
              last={i === cursors.length - 1}
              onOlder={(before) => setCursors((prev) => [...prev, before])}
            />
          ))}
        </ul>
      </Section>
      <div ref={detailRef} tabIndex={-1} role="region" aria-label="The chosen readout" className="min-w-0 scroll-mt-4 outline-none lg:col-span-7">
        {selected !== null ? (
          <ReadoutDetail client={client} dashboard={dashboard} seq={selected} />
        ) : (
          <p className="rounded-xl border border-dashed border-border-strong p-6 text-sm text-muted-foreground">Choose a readout to read it here.</p>
        )}
      </div>
    </div>
  );
}
