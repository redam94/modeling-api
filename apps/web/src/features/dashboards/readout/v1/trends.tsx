/**
 * Trends: the published readouts over the last 13, 26 or 52 periods. The index line shows the
 * result against the model (1.00 = as expected) with a mark where the model was updated; one
 * sparkline a channel shows its return on media against break-even.
 */
import { useQueries, useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { InlineMessage } from "@/components/ods";
import { fixed, MISSING } from "@/components/ods/format";
import { IndexLine, Sparkline } from "@/components/readout-charts";
import { SegmentedControl } from "@/components/ui/overlays";

import { dk } from "../keys";
import { MAX_READOUT_PAGE, type ClientDashboard, type DashboardClient, type ReadoutHeader } from "../types";
import { Section, Working } from "./parts";
import { breakevenOf, isHistoryReadout, periodOf, plural, seriesColor, trendIndex, trendRoi, type TrendPoint } from "./readout-model";

const WINDOWS = ["13", "26", "52"] as const;
type Window = (typeof WINDOWS)[number];

/** The newest `n` history readouts (published, ready), newest first, over as many pages as needed. */
async function historyHeaders(client: DashboardClient, n: number): Promise<ReadoutHeader[]> {
  const out: ReadoutHeader[] = [];
  let before: number | undefined;
  for (let page = 0; page < 6 && out.length < n; page++) {
    const res = await client.readoutPage({ limit: MAX_READOUT_PAGE, ...(before !== undefined ? { beforeSeq: before } : {}) });
    out.push(...(res.items ?? []).filter(isHistoryReadout));
    if (res.next_before_seq == null) break;
    before = res.next_before_seq;
  }
  return out.slice(0, n);
}

export function Trends({ client, dashboard }: { client: DashboardClient; dashboard: ClientDashboard }) {
  const [win, setWin] = useState<Window>("13");
  const n = Number(win);
  const period = periodOf(dashboard);
  const headers = useQuery({ queryKey: [...dk.view(client.cacheKey), "trend", n], queryFn: () => historyHeaders(client, n) });
  const list = headers.data ?? [];
  const readouts = useQueries({
    queries: list.map((h) => ({ queryKey: dk.viewReadout(client.cacheKey, h.seq), queryFn: () => client.readout(h.seq), staleTime: Infinity })),
  });
  const loading = headers.isPending || readouts.some((q) => q.isPending);
  const points: TrendPoint[] = list
    .map((h, i) => ({ readout: h, snapshot: readouts[i]?.data?.snapshot ?? null }))
    .reverse();
  const index = trendIndex(points);
  const be = breakevenOf(dashboard.kpi);
  const channels = dashboard.channels ?? [];

  return (
    <div className="grid min-w-0 gap-6" data-tab="trends">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="max-w-[68ch] text-sm text-muted-foreground">
          Each point is one published readout. 1.00 means that the result was as the model expected. Above 1.00, the result was better.
        </p>
        <SegmentedControl<Window>
          label="Show the last"
          value={win}
          onValueChange={setWin}
          options={WINDOWS.map((w) => ({ value: w, label: `${w} ${plural(period, Number(w))}` }))}
        />
      </div>
      {headers.isError ? <InlineMessage tone="error">We could not load the readouts. Try again later.</InlineMessage> : null}
      {loading && !headers.isError ? <Working>Loading the readouts…</Working> : null}
      {!loading && points.length < 2 ? (
        <InlineMessage tone="info">Trends show when there are two published readouts or more.</InlineMessage>
      ) : null}
      {!loading && points.length >= 2 ? (
        <>
          <Section title="Result compared with the model" description={`The last ${points.length} ${plural(period, points.length)}. The shaded band is the likely range.`}>
            <IndexLine label="Result compared with the model" points={index.points} versions={index.versions} period={period} />
          </Section>
          {be !== null && channels.length ? (
            <Section title="Return on media by channel" description={`What each 1 unit of spend brought. The dashed line is break-even (${fixed(be, 2)}).`}>
              <ul className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
                {channels.map((c, i) => {
                  const values = trendRoi(points, c.name);
                  const last = values.findLast((v) => v !== null);
                  return (
                    <li key={c.name} className="grid min-w-0 gap-1">
                      <div className="flex items-baseline justify-between gap-2">
                        <span className="truncate text-sm font-semibold text-foreground">{c.label}</span>
                        <span className="text-sm text-foreground tabular">{last != null ? fixed(last, 2) : MISSING}</span>
                      </div>
                      <Sparkline label={`${c.label} return on media`} values={values} reference={be} color={`series-${seriesColor(i)}`} />
                    </li>
                  );
                })}
              </ul>
            </Section>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
