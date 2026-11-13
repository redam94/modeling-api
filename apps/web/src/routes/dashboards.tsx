/**
 * Client dashboards (v0.16), the analyst's side: the project's dashboards, and the builder of
 * one dashboard (Setup, Inputs, Access, History, Preview). A draft changes only for the team;
 * publishing pins it as a revision that clients and share links see (plan §3.6.3).
 */
import { getRouteApi, Link, useNavigate } from "@tanstack/react-router";
import { Archive, Plus, RotateCcw, Save, Send } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { can } from "@/api/access";
import { useProjectScope } from "@/api/queries";
import type { Issue } from "@/api/types";
import { EmptyArt } from "@/components/brand/empty-art";
import { EmptyState, ErrorNotice, PageHeader, QueryView, Spinner } from "@/components/common";
import { ListNoMatches, ListToolbar, matchesQuery } from "@/components/list-toolbar";
import { InlineMessage, TagWord, type TagTone } from "@/components/ods";
import { Button } from "@/components/ui/button";
import { FilterPills } from "@/components/ui/filter-pills";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle, Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/overlays";
import { Card, CardContent, TBody, TD, TH, THead, TR, Table } from "@/components/ui/primitives";
import { useWorkspace } from "@/features/auth/workspace";
import { useArchiveDashboard, useDashboard, useDashboards, useMemberClient } from "@/features/dashboards/api";
import { AccessTab } from "@/features/dashboards/builder/access";
import { HistoryTab } from "@/features/dashboards/builder/history";
import { InputsTab } from "@/features/dashboards/builder/inputs";
import { NewDashboardDialog } from "@/features/dashboards/builder/new-dashboard-dialog";
import { PreviewTab } from "@/features/dashboards/builder/preview";
import { PublishDialog } from "@/features/dashboards/builder/publish-dialog";
import { SetupTab } from "@/features/dashboards/builder/setup";
import { UpgradeBanner } from "@/features/dashboards/builder/upgrade-banner";
import { useSourceGrid } from "@/features/dashboards/builder/source";
import { publishPermission, specIssues, tabOfIssue } from "@/features/dashboards/builder/spec-form";
import { useDraft } from "@/features/dashboards/builder/use-draft";
import { ChatLauncher } from "@/features/dashboards/chat/chat-launcher";
import { memberChatTransport } from "@/features/dashboards/chat/transport-member";
import { ConnectChatToolButton } from "@/features/dashboards/connect/connect-button";
import { STATUS_WORD } from "@/features/dashboards/readout/readout-model";
import { dashboardProblemText, type Dashboard, type DashboardStatus, type DashboardSummary } from "@/features/dashboards/types";
import { useRecordRecent } from "@/features/search/use-record-recent";
import { absoluteTime, relativeTime } from "@/lib/format";
import { DASHBOARD_TABS, type DashboardTab } from "@/router";
import { toast } from "@/stores/toast";

const listRoute = getRouteApi("/dashboards");
const detailRoute = getRouteApi("/dashboards/$dashboardId");

const STATUS_TAG: Record<DashboardStatus, { label: string; tone: TagTone }> = {
  draft: { label: "Draft", tone: "neutral" },
  published: { label: "Published", tone: "positive" },
  archived: { label: "Archived", tone: "neutral" },
};

const TAB_LABEL: Record<DashboardTab, string> = { setup: "Setup", inputs: "Inputs", access: "Access", history: "History", preview: "Preview" };

function StatusTag({ status }: { status: DashboardStatus }) {
  return <TagWord tone={STATUS_TAG[status].tone}>{STATUS_TAG[status].label}</TagWord>;
}

// ------------------------------------------------------------------------------ the list

export function DashboardsPage() {
  const { status, new: openNew, model, q: query } = listRoute.useSearch();
  const q = query ?? "";
  const navigate = useNavigate({ from: "/dashboards" });
  const ws = useWorkspace();
  const canCreate = can(ws.grants, "create", "dashboard");
  const dashboards = useDashboards(status ? { status } : {});
  // v0.18: `?new=1&model=<id>` (the checklist, the dashboard tour) opens the New dashboard
  // dialog with that model as the source.
  const [creating, setCreatingState] = useState(() => Boolean(openNew) && canCreate);
  useEffect(() => {
    if (openNew && canCreate) setCreatingState(true);
  }, [openNew, canCreate]);
  const setCreating = (open: boolean) => {
    setCreatingState(open);
    if (!open && (openNew || model)) void navigate({ search: (s) => ({ ...s, new: undefined, model: undefined }), replace: true });
  };
  const setQuery = (next: string) => void navigate({ search: (s) => ({ ...s, q: next || undefined }), replace: true });
  const items = useMemo(() => dashboards.data?.items ?? [], [dashboards.data]);
  const rows = useMemo(() => (q ? items.filter((d) => matchesQuery(q, [d.title, d.name], [d.client_name])) : items), [items, q]);
  const more = Boolean(dashboards.data?.next_cursor);
  const pills = (
    <FilterPills<"active" | DashboardStatus>
      label="Dashboard status"
      value={status ?? "active"}
      onValueChange={(v) => void navigate({ search: (s) => ({ ...s, status: v === "active" ? undefined : v }), replace: true })}
      options={[
        { value: "active", label: "Active" },
        { value: "draft", label: "Drafts" },
        { value: "published", label: "Published" },
        { value: "archived", label: "Archived" },
      ]}
    />
  );
  return (
    <>
      <PageHeader
        title="Dashboards"
        description="Readouts for your clients in plain words, for any model: the latest results, trends, cases to try and new numbers. Clients sign in to see only the dashboards that you give them, or open a share link."
        actions={
          <Button onClick={() => setCreating(true)} disabled={!canCreate} title={canCreate ? undefined : "Only editors and admins create dashboards."}>
            <Plus /> New dashboard
          </Button>
        }
      />
      <div className="grid gap-3">
        {/* Outside the query view: the status pills stay in reach when a list is empty or loads. */}
        <ListToolbar things="dashboards" value={q} onChange={setQuery} count={dashboards.data ? { shown: rows.length, total: items.length, more } : undefined}>
          {pills}
        </ListToolbar>
        <QueryView query={dashboards} errorTitle="The app could not load the dashboards">
          {(page) =>
            rows.length ? (
              <Card>
                <CardContent className="min-w-0 overflow-x-auto pt-4">
                  <Table aria-label="Dashboards">
                    <THead>
                      <TR>
                        <TH>Dashboard</TH>
                        <TH>Status</TH>
                        <TH>Latest readout</TH>
                        <TH>Changed</TH>
                      </TR>
                    </THead>
                    <TBody>
                      {rows.map((d) => (
                        <DashboardRow key={d.id} d={d} />
                      ))}
                    </TBody>
                  </Table>
                </CardContent>
              </Card>
            ) : page.items.length ? (
              <ListNoMatches things="dashboards" query={q} filtered={Boolean(status)} firstPage={more ? page.items.length : undefined} onClear={() => setQuery("")} />
            ) : (
              <EmptyState
                illustration={<EmptyArt kind="dashboards" />}
                title={status === "archived" ? "No archived dashboards" : "This project has no dashboards"}
                action={canCreate && status !== "archived" ? <Button onClick={() => setCreating(true)}>New dashboard</Button> : null}
              >
                {canCreate ? "Make a dashboard from an endpoint or a fitted model. Then publish it and give your client access." : "An editor or an admin makes dashboards."}
              </EmptyState>
            )
          }
        </QueryView>
      </div>
      {creating ? (
        <NewDashboardDialog
          initialModel={openNew && model ? model : undefined}
          onClose={() => setCreating(false)}
          onCreated={(d) => {
            setCreating(false);
            toast.success("Dashboard created", d.draft_spec.title);
            void navigate({ to: "/dashboards/$dashboardId", params: { dashboardId: d.id } });
          }}
        />
      ) : null}
    </>
  );
}

function DashboardRow({ d }: { d: DashboardSummary }) {
  const r = d.latest_readout;
  return (
    <TR data-dashboard={d.id}>
      <TD className="min-w-56">
        <Link to="/dashboards/$dashboardId" params={{ dashboardId: d.id }} className="link font-medium [overflow-wrap:anywhere]">
          {d.title}
        </Link>
        <span className="block text-xs text-muted-foreground [overflow-wrap:anywhere]">
          {d.client_name ? `${d.client_name} · ` : ""}
          {d.name}
        </span>
      </TD>
      <TD>
        <span className="flex flex-wrap gap-1">
          <StatusTag status={d.status} />
          {d.live_inputs === "paused" ? <TagWord tone="warning">Inputs paused</TagWord> : null}
        </span>
      </TD>
      <TD className="text-sm">
        {r ? (
          <>
            <span className="block [overflow-wrap:anywhere]">{r.title}</span>
            <span className="block text-xs text-muted-foreground">{STATUS_WORD[r.status]}</span>
          </>
        ) : (
          <span className="text-muted-foreground">None yet</span>
        )}
      </TD>
      <TD title={absoluteTime(d.updated_at)}>{relativeTime(d.updated_at)}</TD>
    </TR>
  );
}

// ------------------------------------------------------------------------------ one dashboard

export function DashboardPage() {
  const { dashboardId } = detailRoute.useParams();
  const dashboard = useDashboard(dashboardId);
  const d = dashboard.data;
  useRecordRecent(
    d ? { group: "dashboards", title: d.draft_spec.title || d.name, secondary: [STATUS_TAG[d.status].label, d.draft_spec.client_name].filter(Boolean).join(" · "), href: `/dashboards/${d.id}` } : null,
  );
  return (
    <QueryView query={dashboard} errorTitle="The app could not load the dashboard">
      {(d) => <Builder key={d.id} dashboard={d} />}
    </QueryView>
  );
}

function Builder({ dashboard }: { dashboard: Dashboard }) {
  const { tab } = detailRoute.useSearch();
  const navigate = useNavigate({ from: "/dashboards/$dashboardId" });
  const ws = useWorkspace();
  const draft = useDraft(dashboard);
  const grid = useSourceGrid(draft.spec.source);
  const issues = useMemo(() => specIssues(draft.spec, { gridChannels: grid.channels }), [draft.spec, grid.channels]);
  // what the server refuses on save (the grid checks only block publishing)
  const shapeIssues = useMemo(() => specIssues(draft.spec), [draft.spec]);
  const [publishing, setPublishing] = useState(false);
  const [archiving, setArchiving] = useState(false);
  const canEdit = can(ws.grants, "update", "dashboard") && dashboard.status !== "archived";
  const perm = publishPermission(ws.grants.projectRole);
  const open: DashboardTab = tab ?? "setup";
  const setTab = (t: DashboardTab) => void navigate({ search: { tab: t === "setup" ? undefined : t }, replace: true });
  const changedSincePublish = dashboard.published_revision ? JSON.stringify(dashboard.published_revision.spec) !== JSON.stringify(dashboard.draft_spec) : true;

  const save = async () => {
    const out = await draft.save();
    if (out) toast.success("Draft saved", "Clients see the changes after you publish.");
  };
  const publishBlocked = draft.dirty ? "Save the draft first." : dashboard.status === "archived" ? "The dashboard is archived." : perm.reason;
  const goToIssue = (i: Issue) => setTab(tabOfIssue(i.pointer));
  const spec = draft.spec;

  return (
    <>
      <PageHeader
        crumbs={[{ label: "Dashboards", to: "/dashboards" }, { label: spec.title || dashboard.name }]}
        title={spec.title || dashboard.name}
        description={spec.client_name ? `For ${spec.client_name}` : undefined}
        meta={
          <>
            <StatusTag status={dashboard.status} />
            {dashboard.published_revision ? <span>Revision {dashboard.published_revision.seq} published {relativeTime(dashboard.published_revision.created_at)}</span> : null}
            {dashboard.status === "published" && changedSincePublish && !draft.dirty ? <TagWord tone="info">Draft has changes to publish</TagWord> : null}
            {draft.dirty ? <TagWord tone="warning">Unsaved changes</TagWord> : null}
          </>
        }
        actions={
          <>
            {draft.dirty ? (
              <Button variant="ghost" onClick={() => void draft.reset()} disabled={draft.saving}>
                <RotateCcw /> Discard
              </Button>
            ) : null}
            <Button variant="secondary" disabled={!canEdit || !draft.dirty || draft.saving || shapeIssues.length > 0} title={shapeIssues.length ? `Fix ${shapeIssues.length} ${shapeIssues.length === 1 ? "problem" : "problems"} first.` : undefined} onClick={() => void save()}>
              {draft.saving ? <Spinner /> : <Save />} Save draft
            </Button>
            <Button disabled={!canEdit || Boolean(publishBlocked)} title={publishBlocked ?? undefined} onClick={() => setPublishing(true)} data-tour="dashboards.publish">
              <Send /> {dashboard.status === "published" ? "Publish changes" : "Publish"}
            </Button>
            {dashboard.status !== "archived" && can(ws.grants, "delete", "dashboard") ? (
              <Button variant="ghost" onClick={() => setArchiving(true)} aria-label="Archive the dashboard">
                <Archive />
              </Button>
            ) : null}
          </>
        }
      />
      <div className="grid min-w-0 gap-4">
        {dashboard.live_inputs === "paused" ? (
          <InlineMessage tone="warning" role="none">
            Live inputs are paused{dashboard.problem ? `: ${dashboard.problem}` : "."} Clients see the last readout. Publish again as a member with the runner role to start them again.
          </InlineMessage>
        ) : null}
        <UpgradeBanner spec={spec} readOnly={!canEdit} />
        {dashboard.problem && dashboard.live_inputs !== "paused" && dashboard.problem !== "upgrade_needs_review" ? (
          <InlineMessage tone="warning" role="none">
            {dashboardProblemText(dashboard.problem)}
          </InlineMessage>
        ) : null}
        {draft.stale ? (
          <InlineMessage
            tone="warning"
            action={
              <Button size="sm" variant="secondary" onClick={() => void draft.reset()}>
                Load the new draft
              </Button>
            }
          >
            Another person changed this draft. Load their version, then make your changes again.
          </InlineMessage>
        ) : null}
        {draft.saveError && !draft.stale ? <ErrorNotice error={draft.saveError} title="The draft was not saved" /> : null}
        {!canEdit && dashboard.status !== "archived" ? (
          <InlineMessage tone="info" role="none">
            You can look at this dashboard. Only editors and admins change it.
          </InlineMessage>
        ) : null}
        <Tabs value={open} onValueChange={(v) => setTab(v as DashboardTab)}>
          <TabsList aria-label="Dashboard builder">
            {DASHBOARD_TABS.map((t) => {
              const n = t === "setup" || t === "inputs" ? issues.filter((i) => tabOfIssue(i.pointer) === t).length : 0;
              return (
                <TabsTrigger key={t} value={t} data-tour={t === "access" ? "dashboards.share" : undefined}>
                  {TAB_LABEL[t]}
                  {n ? (
                    <span className="ml-1 text-xs text-destructive" aria-label={`${n} problems`}>
                      ({n})
                    </span>
                  ) : null}
                </TabsTrigger>
              );
            })}
          </TabsList>
          <TabsContent value="setup" data-tour="dashboards.builder.panels">
            <SetupTab spec={spec} onChange={draft.set} issues={issues} grid={grid} readOnly={!canEdit} />
          </TabsContent>
          <TabsContent value="inputs">
            <InputsTab spec={spec} onChange={draft.set} issues={issues} grid={grid} readOnly={!canEdit} />
          </TabsContent>
          <TabsContent value="access">
            <AccessTab dashboard={dashboard} />
          </TabsContent>
          <TabsContent value="history">
            <HistoryTab dashboard={dashboard} />
          </TabsContent>
          <TabsContent value="preview">
            {dashboard.status === "published" ? (
              <div className="mb-3 flex justify-end">
                <ConnectChatToolButton dashboardId={dashboard.id} title={dashboard.published_revision?.spec.title ?? dashboard.name} />
              </div>
            ) : null}
            <PreviewTab dashboardId={dashboard.id} dirty={draft.dirty} />
            {dashboard.status === "published" && open === "preview" ? <BuilderChat dashboardId={dashboard.id} /> : null}
          </TabsContent>
        </Tabs>
      </div>
      {publishing ? <PublishDialog dashboard={dashboard} onClose={() => setPublishing(false)} onIssue={goToIssue} /> : null}
      {archiving ? <ArchiveDialog dashboard={dashboard} onClose={() => setArchiving(false)} /> : null}
    </>
  );
}

/**
 * v0.20: "Ask about your dashboard" beside the preview, as the signed-in member (chat reads
 * the published revision; the launcher shows nothing until an editor turns chat on).
 */
function BuilderChat({ dashboardId }: { dashboardId: string }) {
  const scope = useProjectScope();
  const client = useMemberClient(dashboardId);
  const transport = useMemo(() => memberChatTransport(dashboardId, { scopeKey: scope.key }), [dashboardId, scope.key]);
  return <ChatLauncher transport={transport} cacheKey={client.cacheKey} />;
}

function ArchiveDialog({ dashboard, onClose }: { dashboard: Dashboard; onClose: () => void }) {
  const archive = useArchiveDashboard(dashboard.id);
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogTitle>Archive “{dashboard.draft_spec.title}”?</DialogTitle>
        <DialogDescription>Clients stop seeing it, and every share link ends now. The readouts stay for your team. You cannot undo this.</DialogDescription>
        {archive.isError ? <ErrorNotice error={archive.error} /> : null}
        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>
            Keep it
          </Button>
          <Button
            variant="destructive"
            disabled={archive.isPending}
            onClick={() =>
              archive.mutate(undefined, {
                onSuccess: () => {
                  toast.success("Dashboard archived", dashboard.draft_spec.title);
                  onClose();
                },
              })
            }
          >
            <Archive /> Archive
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
