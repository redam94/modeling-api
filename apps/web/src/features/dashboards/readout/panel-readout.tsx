/**
 * The layout 2 readout (v0.17, plan §3.7): the dashboard of any model type, as a client user,
 * a share link and the analyst's preview see it. The header is the layout 1 one (title, meta
 * line, "Download summary (.csv)"); then the sections the viewer may use:
 *
 * Overview (the panels in the analyst's order: KPI tiles, then one card per chart, table, map,
 * picture or note) · Try a case · Check accuracy · Score a file · History.
 *
 * The media planner preset (`mmm_planner`) keeps its planner (the layout 1 tabs, with the
 * analyst's extra panels under This period). An automatically upgraded dashboard shows a
 * one-time plain note that each viewer can dismiss (remembered in this browser; for a share
 * link only while the page is open, plan §10.1).
 *
 * Like the layout 1 page, it reads only through its {@link DashboardClient} and imports nothing
 * of the signed-in app. The heavy renderers (`panels/readout-extra`) and the three input
 * sections load lazily. WP13's builder preview renders this component with `preview`.
 */
import { ChevronRight, Download, X } from "lucide-react";
import { lazy, useCallback, useEffect, useRef, useState } from "react";
import type * as React from "react";

import { Eyebrow, InlineMessage } from "@/components/ods";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/overlays";

import { hasPanels, type ClientDashboard, type DashboardClient, type Readout, type SectionKey, type TabKey } from "../types";
import { History } from "./history";
import { LazyPart } from "./load-boundary";
import { asClientV2, dismissUpgradeNote, pageSections, SECTION_LABEL, sectionOfTab, summaryCsvV2, upgradeNoteDismissed } from "./panel-model";
import { PanelBlocks } from "./panels/blocks";
import { KpiChart, ReadoutState, ReadoutTiles, saveText } from "./parts";
import { metaLine, periodOf, summaryFileName } from "./readout-model";
import { PlannerBody } from "./v1/planner-body";
import { TechnicalToggle } from "./v1/technical-toggle";

const TryACase = lazy(() => import("./try-a-case").then((m) => ({ default: m.TryACase })));
const CheckAccuracy = lazy(() => import("./check-accuracy").then((m) => ({ default: m.CheckAccuracy })));
const ScoreFile = lazy(() => import("./score-file").then((m) => ({ default: m.ScoreFile })));

export interface PanelReadoutProps {
  client: DashboardClient;
  dashboard: ClientDashboard;
  /** The analyst's preview: inputs are shown but cannot send anything; no upgrade note. */
  preview?: boolean;
  /** The readout to show (the builder preview's pick); the dashboard's latest when left out. */
  readout?: Readout | null;
  /** The open section (controlled); uncontrolled when left out. */
  section?: SectionKey | undefined;
  onSectionChange?: ((section: SectionKey) => void) | undefined;
  /** A layout 1 tab (`?tab=` from before the upgrade): the planner opens it; other presets map it. */
  tab?: TabKey | undefined;
  onTabChange?: ((tab: TabKey) => void) | undefined;
}

/** The one-time note of an automatically upgraded dashboard; "Close" hides it for this viewer. */
export function UpgradeNote({ client, dashboard, preview = false }: { client: DashboardClient; dashboard: ClientDashboard; preview?: boolean }) {
  const note = dashboard.upgrade_note ?? null;
  const [closed, setClosed] = useState(() => (note ? upgradeNoteDismissed(client.kind, dashboard.id, note) : true));
  if (!note || preview || closed) return null;
  return (
    <InlineMessage
      tone="info"
      role="none"
      className="max-w-full"
      action={
        <Button
          variant="ghost"
          size="sm"
          onClick={() => {
            dismissUpgradeNote(client.kind, dashboard.id, note);
            setClosed(true);
          }}
        >
          <X /> Close
        </Button>
      }
    >
      <span data-readout="upgrade-note">{note}</span>
    </InlineMessage>
  );
}

function Lazy({ children }: { children: React.ReactNode }) {
  return <LazyPart>{children}</LazyPart>;
}

function Overview({ client, dashboard, readout }: { client: DashboardClient; dashboard: ClientDashboard; readout: Readout | null }) {
  if (!readout) return <p className="rounded-xl border border-dashed border-border-strong p-6 text-sm text-muted-foreground">There are no results yet. Your analyst publishes the first readout soon.</p>;
  const snap = readout.status === "ready" ? (readout.snapshot ?? null) : null;
  return (
    <div className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-6" data-tab="overview">
      <ReadoutState readout={readout} />
      {snap ? (
        <>
          <p className="max-w-[72ch] text-lg text-foreground" data-readout="lead">
            {snap.lead}
          </p>
          {hasPanels(snap) ? (
            <PanelBlocks client={client} dashboard={dashboard} snapshot={snap} />
          ) : (
            <>
              <ReadoutTiles dashboard={dashboard} readout={readout} />
              <KpiChart dashboard={dashboard} readout={readout} />
            </>
          )}
          {snap.notes_plain?.length ? (
            <ul className="grid gap-1 text-xs text-hint">
              {snap.notes_plain.map((n) => (
                <li key={n}>{n}</li>
              ))}
            </ul>
          ) : null}
          {dashboard.show_technical ? <TechnicalToggle readout={readout} /> : null}
        </>
      ) : null}
    </div>
  );
}

function PlannerReadout({ client, dashboard, preview, tab, onTabChange }: Pick<PanelReadoutProps, "client" | "dashboard" | "tab" | "onTabChange"> & { preview: boolean }) {
  const [own, setOwn] = useState<TabKey>("this_period");
  const snap = dashboard.latest_readout?.status === "ready" ? (dashboard.latest_readout.snapshot ?? null) : null;
  return (
    <PlannerBody
      client={client}
      dashboard={dashboard}
      preview={preview}
      tab={tab ?? own}
      onTab={(t) => {
        setOwn(t);
        onTabChange?.(t);
      }}
      notice={<UpgradeNote client={client} dashboard={dashboard} preview={preview} />}
      extra={
        snap?.panels?.length ? (
          <div className="mt-6 grid min-w-0 grid-cols-[minmax(0,1fr)] gap-6">
            <PanelBlocks client={client} dashboard={dashboard} snapshot={snap} planner label="More numbers" />
          </div>
        ) : null
      }
    />
  );
}

/**
 * The section tabs. On a phone they scroll sideways inside their own row (never the page), with a
 * fade and a "more" arrow on the side that has hidden tabs, and the open tab scrolled into view.
 */
function SectionTabs({ open, children }: { open: string; children: React.ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  const [more, setMore] = useState({ left: false, right: false });
  const measure = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    const left = el.scrollLeft > 1;
    const right = el.scrollLeft + el.clientWidth < el.scrollWidth - 1;
    setMore((m) => (m.left === left && m.right === right ? m : { left, right }));
  }, []);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    measure();
    el.querySelector<HTMLElement>('[data-state="active"]')?.scrollIntoView?.({ block: "nearest", inline: "nearest" });
    const ro = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    ro?.observe(el);
    return () => ro?.disconnect();
  }, [open, measure]);
  return (
    <div className="relative min-w-0" data-tabs="sections" data-more-left={more.left || undefined} data-more-right={more.right || undefined}>
      <TabsList ref={ref} aria-label="Dashboard sections" className="w-full gap-5 sm:gap-8" onScroll={measure}>
        {children}
      </TabsList>
      {more.left ? <span aria-hidden className="pointer-events-none absolute inset-y-0 left-0 w-8 bg-gradient-to-r from-background to-transparent" /> : null}
      {more.right ? (
        <span aria-hidden data-more="right" className="pointer-events-none absolute inset-y-0 right-0 flex w-10 items-center justify-end bg-gradient-to-l from-background via-background/80 to-transparent text-muted-foreground">
          <ChevronRight className="size-4" />
        </span>
      ) : null}
    </div>
  );
}

export function PanelReadout({ client, dashboard: given, preview = false, readout, section, onSectionChange, tab, onTabChange }: PanelReadoutProps) {
  const dashboard: ClientDashboard = readout !== undefined ? { ...given, latest_readout: readout } : given;
  const [own, setOwn] = useState<SectionKey>(() => sectionOfTab(tab) ?? "overview");
  if (dashboard.preset === "mmm_planner") return <PlannerReadout client={client} dashboard={dashboard} preview={preview} tab={tab} onTabChange={onTabChange} />;
  const v2 = asClientV2(client);
  const latest = dashboard.latest_readout ?? null;
  const period = periodOf(dashboard);
  const sections = pageSections(dashboard).filter((s) => !((s === "try" || s === "batch") && !v2));
  const current = section ?? own;
  const open = sections.includes(current) ? current : "overview";
  const choose = (s: SectionKey) => {
    setOwn(s);
    onSectionChange?.(s);
  };
  return (
    <div className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-6" data-layout="2" data-preset={dashboard.preset ?? undefined}>
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div className="grid min-w-0 gap-2">
          {dashboard.client_name ? <Eyebrow>{dashboard.client_name}</Eyebrow> : null}
          <h1 className="text-3xl font-semibold tracking-tight text-foreground">{dashboard.title}</h1>
          {dashboard.summary ? <p className="max-w-[72ch] text-base text-muted-foreground">{dashboard.summary}</p> : null}
          <p className="text-sm text-hint" data-readout="meta">
            {metaLine(latest, period)}
          </p>
        </div>
        <Button variant="secondary" disabled={!latest?.snapshot} onClick={() => saveText(summaryFileName(dashboard.title, latest), summaryCsvV2(dashboard, latest))}>
          <Download /> Download summary (.csv)
        </Button>
      </header>
      <UpgradeNote client={client} dashboard={dashboard} preview={preview} />
      {dashboard.note ? <p className="max-w-[72ch] text-sm whitespace-pre-line text-muted-foreground">{dashboard.note}</p> : null}
      <Tabs value={open} onValueChange={(v) => choose(v as SectionKey)}>
        {sections.length > 1 ? (
          <SectionTabs open={open}>
            {sections.map((s) => (
              <TabsTrigger key={s} value={s}>
                {SECTION_LABEL[s]}
              </TabsTrigger>
            ))}
          </SectionTabs>
        ) : null}
        <TabsContent value="overview">
          <Overview client={client} dashboard={dashboard} readout={latest} />
        </TabsContent>
        {v2 && sections.includes("try") ? (
          <TabsContent value="try">
            <Lazy>
              <TryACase client={v2} dashboard={dashboard} preview={preview} />
            </Lazy>
          </TabsContent>
        ) : null}
        {sections.includes("accuracy") ? (
          <TabsContent value="accuracy">
            <Lazy>
              <CheckAccuracy client={client} dashboard={dashboard} preview={preview} />
            </Lazy>
          </TabsContent>
        ) : null}
        {v2 && sections.includes("batch") ? (
          <TabsContent value="batch">
            <Lazy>
              <ScoreFile client={v2} dashboard={dashboard} preview={preview} />
            </Lazy>
          </TabsContent>
        ) : null}
        {sections.includes("history") ? (
          <TabsContent value="history">
            <History client={client} dashboard={dashboard} description="The newest first. The cases you tried, the files you scored and the results you sent show only to you and the analysts." />
          </TabsContent>
        ) : null}
      </Tabs>
    </div>
  );
}
