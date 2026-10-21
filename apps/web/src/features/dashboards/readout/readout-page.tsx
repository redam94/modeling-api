/**
 * The readout page of a client dashboard (plan §3.6.2): one page for a client user, a share link
 * and the analyst's preview. It reads only through its {@link DashboardClient}, so it never
 * knows who is looking, and it imports nothing of the signed-in app (no `api/queries.ts`, no
 * router), so the share app can load it on its own.
 *
 * MEI planner composition: the title, a meta line ("Updated 20/10/2026 09.40 with results to the
 * week ending 18/10/2026") and one action ("Download summary (.csv)"), then the tabs the
 * dashboard has and the viewer may use.
 */
import { Download } from "lucide-react";
import { useState } from "react";

import { Eyebrow, InlineMessage } from "@/components/ods";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/overlays";
import { Skeleton } from "@/components/ui/primitives";

import { dashboardErrorMessage, problemOf, useDashboardView, useReadout, useReadoutList } from "../client";
import type { ClientDashboard, DashboardClient, Readout, ReadoutHeader, TabKey } from "../types";
import { History } from "./history";
import { saveText } from "./parts";
import { isHistoryReadout, metaLine, pageTabs, periodOf, summaryCsv, summaryFileName, tabLabel } from "./readout-model";
import { Scenarios } from "./scenarios";
import { ThisPeriod } from "./this-period";
import { Trends } from "./trends";
import { UpdateNumbers } from "./update-numbers";

export interface ReadoutPageProps {
  client: DashboardClient;
  /** The analyst's preview: inputs are shown but cannot send anything. */
  preview?: boolean;
  /** The open tab (controlled, e.g. from `?tab=`); uncontrolled when left out. */
  tab?: TabKey | undefined;
  onTabChange?: ((tab: TabKey) => void) | undefined;
}

function PageSkeleton() {
  return (
    <div className="grid gap-4" role="status" aria-label="Loading the dashboard">
      <Skeleton className="h-9 w-2/3 max-w-xl" />
      <Skeleton className="h-4 w-1/2 max-w-md" />
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {[0, 1, 2, 3].map((i) => (
          <Skeleton key={i} className="h-28" />
        ))}
      </div>
      <Skeleton className="h-64" />
    </div>
  );
}

/** The readout before the latest (for "What changed") and the uploads waiting for review. */
function useReadoutContext(client: DashboardClient, latest: Readout | null) {
  const list = useReadoutList(client, { limit: 20 });
  const items: ReadoutHeader[] = list.data?.items ?? [];
  const prev = latest ? (items.filter((h) => isHistoryReadout(h) && h.seq < latest.seq).sort((a, b) => b.seq - a.seq)[0] ?? null) : null;
  const previous = useReadout(client, prev?.seq ?? null);
  const pending = items.filter((h) => h.visibility === "pending_review" && h.status !== "failed");
  return { previous: previous.data ?? null, pending };
}

function Body({ client, dashboard, preview, tab, onTab }: { client: DashboardClient; dashboard: ClientDashboard; preview: boolean; tab: TabKey; onTab: (t: TabKey) => void }) {
  const latest = dashboard.latest_readout ?? null;
  const { previous, pending } = useReadoutContext(client, latest);
  const tabs = pageTabs(dashboard);
  const period = periodOf(dashboard);
  const open = tabs.includes(tab) ? tab : "this_period";
  return (
    <div className="grid min-w-0 gap-6">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div className="grid min-w-0 gap-2">
          {dashboard.client_name ? <Eyebrow>{dashboard.client_name}</Eyebrow> : null}
          <h1 className="text-3xl font-semibold tracking-tight text-foreground">{dashboard.title}</h1>
          {dashboard.summary ? <p className="max-w-[72ch] text-base text-muted-foreground">{dashboard.summary}</p> : null}
          <p className="text-sm text-hint" data-readout="meta">
            {metaLine(latest, period)}
          </p>
        </div>
        <Button variant="secondary" disabled={!latest?.snapshot} onClick={() => saveText(summaryFileName(dashboard.title, latest), summaryCsv(dashboard, latest))}>
          <Download /> Download summary (.csv)
        </Button>
      </header>
      <Tabs value={open} onValueChange={(v) => onTab(v as TabKey)}>
        {tabs.length > 1 ? (
          <TabsList aria-label="Dashboard sections" className="w-full">
            {tabs.map((t) => (
              <TabsTrigger key={t} value={t}>
                {tabLabel(t, period)}
              </TabsTrigger>
            ))}
          </TabsList>
        ) : null}
        <TabsContent value="this_period">
          <ThisPeriod dashboard={dashboard} readout={latest} previous={previous} pending={pending} preview={preview} />
        </TabsContent>
        {tabs.includes("trends") ? (
          <TabsContent value="trends">
            <Trends client={client} dashboard={dashboard} />
          </TabsContent>
        ) : null}
        {tabs.includes("scenarios") ? (
          <TabsContent value="scenarios">
            <Scenarios client={client} dashboard={dashboard} preview={preview} />
          </TabsContent>
        ) : null}
        {tabs.includes("update") ? (
          <TabsContent value="update">
            <UpdateNumbers client={client} dashboard={dashboard} preview={preview} />
          </TabsContent>
        ) : null}
        {tabs.includes("history") ? (
          <TabsContent value="history">
            <History client={client} dashboard={dashboard} />
          </TabsContent>
        ) : null}
      </Tabs>
    </div>
  );
}

export function ReadoutPage({ client, preview = false, tab, onTabChange }: ReadoutPageProps) {
  const q = useDashboardView(client);
  const [own, setOwn] = useState<TabKey>("this_period");
  const current = tab ?? own;
  const onTab = (t: TabKey) => {
    setOwn(t);
    onTabChange?.(t);
  };
  if (q.isPending) return <PageSkeleton />;
  if (q.isError) {
    const p = problemOf(q.error);
    return (
      <InlineMessage tone="error">
        {p?.status === 404 ? "This dashboard is not available. It may have been removed, or it was not shared with you. Ask your analyst." : dashboardErrorMessage(q.error)}
      </InlineMessage>
    );
  }
  return <Body client={client} dashboard={q.data} preview={preview} tab={current} onTab={onTab} />;
}
