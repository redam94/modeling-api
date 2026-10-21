/**
 * The layout 1 readout body (v0.16, moved here unchanged from `readout-page.tsx`): the title,
 * the meta line, "Download summary (.csv)" and the planner tabs (This period, Trends, Plan
 * scenarios, Update numbers, History). Layout 1 dashboards and the media planner preset of
 * layout 2 (`mmm_planner`) render it; the planner adds the upgrade note (`notice`) and the
 * analyst's extra panels (`extra`, under This period). Without them the DOM is the v0.16 one.
 */
import { Download } from "lucide-react";
import type * as React from "react";

import { Eyebrow } from "@/components/ods";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/overlays";

import { useReadout, useReadoutList } from "../../client";
import type { ClientDashboard, DashboardClient, Readout, ReadoutHeader, TabKey } from "../../types";
import { History } from "../history";
import { saveText } from "../parts";
import { isHistoryReadout, metaLine, pageTabs, periodOf, summaryCsv, summaryFileName, tabLabel } from "../readout-model";
import { Scenarios } from "./scenarios";
import { ThisPeriod } from "./this-period";
import { Trends } from "./trends";
import { UpdateNumbers } from "./update-numbers";

/** The readout before the latest (for "What changed") and the uploads waiting for review. */
function useReadoutContext(client: DashboardClient, latest: Readout | null) {
  const list = useReadoutList(client, { limit: 20 });
  const items: ReadoutHeader[] = list.data?.items ?? [];
  const prev = latest ? (items.filter((h) => isHistoryReadout(h) && h.seq < latest.seq).sort((a, b) => b.seq - a.seq)[0] ?? null) : null;
  const previous = useReadout(client, prev?.seq ?? null);
  const pending = items.filter((h) => h.visibility === "pending_review" && h.status !== "failed");
  return { previous: previous.data ?? null, pending };
}

export function PlannerBody({
  client,
  dashboard,
  preview,
  tab,
  onTab,
  notice,
  extra,
}: {
  client: DashboardClient;
  dashboard: ClientDashboard;
  preview: boolean;
  tab: TabKey;
  onTab: (t: TabKey) => void;
  /** Above the tabs (the one-time upgrade note of layout 2). */
  notice?: React.ReactNode;
  /** Under This period (the extra panels of a layout 2 planner). */
  extra?: React.ReactNode;
}) {
  const latest = dashboard.latest_readout ?? null;
  const { previous, pending } = useReadoutContext(client, latest);
  const tabs = pageTabs(dashboard);
  const period = periodOf(dashboard);
  const open = tabs.includes(tab) ? tab : "this_period";
  return (
    // minmax(0, 1fr): an implicit `auto` track grows to its widest child's max-content (the
    // header's nowrap button, a tab's wide table), so the page would scroll sideways at 390 px
    <div className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-6">
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
      {notice}
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
          {extra}
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
