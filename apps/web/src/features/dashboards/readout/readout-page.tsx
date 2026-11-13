/**
 * The readout page of a client dashboard (plan §3.6.2): one page for a client user, a share link
 * and the analyst's preview. It reads only through its {@link DashboardClient}, so it never
 * knows who is looking, and it imports nothing of the signed-in app (no `api/queries.ts`, no
 * router), so the share app can load it on its own.
 *
 * Layout 1 (v0.16, MEI planner composition: the title, a meta line and "Download summary
 * (.csv)", then the tabs the dashboard has and the viewer may use) renders the v1 tree in
 * `v1/`. Layout 2 (v0.17: a `schema_version` 2 snapshot or a layout 2 dashboard, plan §3.7)
 * renders {@link PanelReadout}. History mixes both after the upgrade; each readout there is
 * drawn by its own shape.
 *
 * v0.20: a surface that offers "Ask about your dashboard" passes its {@link ChatTransport}
 * (`chat`); the page then shows the chat launcher on a layout 2 dashboard (the launcher shows
 * nothing when chat is not available to this viewer). `connect` is the signed-in "Connect a
 * chat tool" button. Without either prop the page renders exactly as before.
 */
import { useState, type ReactNode } from "react";

import { InlineMessage } from "@/components/ods";
import { Skeleton } from "@/components/ui/primitives";

import { ChatLauncher } from "../chat/chat-launcher";
import type { ChatTransport } from "../chat/types";
import { dashboardErrorMessage, problemOf, useDashboardView } from "../client";
import { readoutLayout, type DashboardClient, type SectionKey, type TabKey } from "../types";
import { PanelReadout } from "./panel-readout";
import { PlannerBody } from "./v1/planner-body";

export interface ReadoutPageProps {
  client: DashboardClient;
  /** The analyst's preview: inputs are shown but cannot send anything. */
  preview?: boolean;
  /** The open tab (controlled, e.g. from `?tab=`); uncontrolled when left out. */
  tab?: TabKey | undefined;
  onTabChange?: ((tab: TabKey) => void) | undefined;
  /** The open layout 2 section (controlled); a layout 1 `tab` is mapped when left out. */
  section?: SectionKey | undefined;
  onSectionChange?: ((section: SectionKey) => void) | undefined;
  /** v0.20: how this surface asks about the dashboard; no chat when left out. */
  chat?: ChatTransport | undefined;
  /** v0.20: the "Connect a chat tool" button (signed-in surfaces only). */
  connect?: ReactNode;
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

export function ReadoutPage({ client, preview = false, tab, onTabChange, section, onSectionChange, chat, connect }: ReadoutPageProps) {
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
  const layout2 = readoutLayout(q.data, q.data.latest_readout ?? null) === 2 || q.data.layout_version === 2;
  const body = layout2 ? (
    <PanelReadout client={client} dashboard={q.data} preview={preview} tab={current} onTabChange={onTab} section={section} onSectionChange={onSectionChange} />
  ) : (
    <PlannerBody client={client} dashboard={q.data} preview={preview} tab={current} onTab={onTab} />
  );
  if (!connect && !chat) return body;
  return (
    <>
      {connect ? <div className="mb-3 flex justify-end">{connect}</div> : null}
      {body}
      {/* Chat needs a layout 2 dashboard (plan decision 5); the server says the same. */}
      {chat && layout2 ? <ChatLauncher transport={chat} cacheKey={client.cacheKey} /> : null}
    </>
  );
}
