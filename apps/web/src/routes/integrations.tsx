/**
 * The Integrations hub (plan §11): Guided cards for spreadsheets, dashboards and webhooks, and
 * the Expert sections - endpoints and aliases, webhooks with their delivery logs, saved plots
 * with static renders and embed links - plus an endpoint's and a webhook's own pages.
 */
import { getRouteApi, Link, useNavigate } from "@tanstack/react-router";
import { ArrowUpCircle, ChevronLeft, KeyRound, RotateCcw, Trash2 } from "lucide-react";
import { useState } from "react";

import { can } from "@/api/access";
import { useEndpoint, useEndpointHistory, useWebhook } from "@/api/integrations";
import type { Endpoint, EndpointRevision } from "@/api/types";
import { KeyValues, Mono, PageHeader, QueryView } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/overlays";
import { Card, CardContent, CardDescription, CardHeader, CardTitle, TBody, TD, TH, THead, TR, Table } from "@/components/ui/primitives";
import { PersonName } from "@/features/auth/people";
import { useWorkspace } from "@/features/auth/workspace";
import { EndpointSchemaPanel, EndpointSnippetsPanel } from "@/features/integrations/endpoint-schema";
import { DeleteEndpointDialog, EndpointsSection, GateBadge, GateReasons, PromoteDialog, RollbackDialog } from "@/features/integrations/endpoints";
import { IntegrationsOverview, type HubTab } from "@/features/integrations/overview";
import { SharingSection } from "@/features/integrations/sharing";
import { AutoDisabledNotice, DeleteWebhookDialog, DeliveryLog, WebhookSettings, WebhookStatusBadge, WebhooksSection } from "@/features/integrations/webhooks";
import { absoluteTime, relativeTime } from "@/lib/format";
import { INTEGRATION_TABS, type EndpointTab } from "@/router";

const hubRoute = getRouteApi("/integrations");
const endpointRoute = getRouteApi("/integrations/endpoints/$alias");
const webhookRoute = getRouteApi("/integrations/webhooks/$webhookId");

const TAB_LABEL: Record<HubTab, string> = { overview: "Overview", endpoints: "Endpoints", webhooks: "Webhooks", sharing: "Plots and links" };

export function IntegrationsPage() {
  const search = hubRoute.useSearch();
  const navigate = useNavigate({ from: "/integrations" });
  const tab = search.tab ?? "overview";
  const goTo = (t: HubTab) => void navigate({ search: { tab: t === "overview" ? undefined : t } });
  const sharePick = search.model ? { model: search.model, version: search.version, plot: search.plot } : undefined;
  return (
    <>
      <PageHeader title="Integrations" description="Use your models outside the platform: spreadsheets, dashboards, notebooks, Atlas and your own services." />
      <Tabs value={tab} onValueChange={(v) => goTo(v as HubTab)}>
        <TabsList aria-label="Integrations sections">
          {INTEGRATION_TABS.map((t) => (
            <TabsTrigger key={t} value={t}>
              {TAB_LABEL[t]}
            </TabsTrigger>
          ))}
        </TabsList>
        <TabsContent value="overview">
          <IntegrationsOverview goTo={goTo} />
        </TabsContent>
        <TabsContent value="endpoints">
          <EndpointsSection />
        </TabsContent>
        <TabsContent value="webhooks">
          <WebhooksSection />
        </TabsContent>
        <TabsContent value="sharing">
          <SharingSection initial={sharePick} onConsumeInitial={() => void navigate({ search: { tab: "sharing" }, replace: true })} />
        </TabsContent>
      </Tabs>
    </>
  );
}

function BackToHub({ tab }: { tab: HubTab }) {
  return (
    <Link to="/integrations" search={{ tab }} className="mb-3 inline-flex items-center gap-1 text-[13px] text-muted-foreground hover:text-foreground">
      <ChevronLeft className="size-4" /> Integrations
    </Link>
  );
}

// ----------------------------------------------------------------------- endpoint

function HistoryTable({ endpoint, onRollback }: { endpoint: Endpoint; onRollback: (r: EndpointRevision) => void }) {
  const history = useEndpointHistory(endpoint.alias);
  const ws = useWorkspace();
  const canRollback = can(ws.grants, "promote", "endpoint");
  return (
    <QueryView query={history}>
      {(page) => (
        <Table aria-label="Endpoint history">
          <THead>
            <TR>
              <TH>Revision</TH>
              <TH>Change</TH>
              <TH>Version</TH>
              <TH>Gate</TH>
              <TH>Reason</TH>
              <TH>When</TH>
              <TH>
                <span className="sr-only">Actions</span>
              </TH>
            </TR>
          </THead>
          <TBody>
            {page.items.map((r) => (
              <TR key={r.revision}>
                <TD className="tabular">
                  r{r.revision}
                  {r.revision === endpoint.revision ? <span className="block text-xs text-success">current</span> : null}
                </TD>
                <TD>{r.action === "rollback" ? "Rollback" : "Promotion"}</TD>
                <TD>
                  <Mono>{r.model_ref}</Mono>
                </TD>
                <TD>
                  <GateBadge outcome={r.gate.outcome} overridden={Boolean(r.overridden)} />
                </TD>
                <TD className="max-w-72 text-[13px] text-muted-foreground">{r.reason ?? "—"}</TD>
                <TD>
                  <span title={absoluteTime(r.created_at)}>{relativeTime(r.created_at)}</span>
                  {r.actor_id ? <PersonName id={r.actor_id} className="block text-[11px] text-muted-foreground" /> : null}
                </TD>
                <TD className="text-right">
                  {r.revision < endpoint.revision ? (
                    <Button variant="ghost" size="sm" disabled={!canRollback} onClick={() => onRollback(r)} aria-label={`Roll back to r${r.revision}`}>
                      <RotateCcw /> Roll back here
                    </Button>
                  ) : null}
                </TD>
              </TR>
            ))}
          </TBody>
        </Table>
      )}
    </QueryView>
  );
}

function EndpointView({ endpoint }: { endpoint: Endpoint }) {
  const ws = useWorkspace();
  const navigate = useNavigate();
  const search = endpointRoute.useSearch();
  const canPromote = can(ws.grants, "promote", "endpoint");
  const canDelete = can(ws.grants, "delete", "endpoint");
  const [promoting, setPromoting] = useState(false);
  const [rollback, setRollback] = useState<EndpointRevision | "previous" | null>(null);
  const [deleting, setDeleting] = useState(false);
  const tab = search.tab ?? "overview";
  return (
    <>
      <PageHeader
        title={endpoint.alias}
        description={endpoint.description ?? undefined}
        meta={
          <>
            <Mono>{endpoint.ref}</Mono>
            <span>· r{endpoint.revision}</span>
          </>
        }
        actions={
          <>
            <Button variant="outline" asChild>
              <Link to="/tokens" search={{ preset: `score:${endpoint.alias}` }}>
                <KeyRound /> Token for this endpoint
              </Link>
            </Button>
            <Button variant="outline" disabled={!canPromote || endpoint.revision < 2} onClick={() => setRollback("previous")}>
              <RotateCcw /> Roll back
            </Button>
            <Button onClick={() => setPromoting(true)} disabled={!canPromote}>
              <ArrowUpCircle /> Promote a version
            </Button>
          </>
        }
      />
      <Tabs value={tab} onValueChange={(v) => void navigate({ to: "/integrations/endpoints/$alias", params: { alias: endpoint.alias }, search: { tab: v === "overview" ? undefined : (v as EndpointTab) } })}>
        <TabsList aria-label="Endpoint sections">
          <TabsTrigger value="overview">Version and history</TabsTrigger>
          <TabsTrigger value="schema">Schema and Try it</TabsTrigger>
          <TabsTrigger value="snippets">Snippets</TabsTrigger>
        </TabsList>
        <TabsContent value="overview" className="grid gap-5">
          <Card>
            <CardHeader>
              <div>
                <CardTitle>Serving now</CardTitle>
                <CardDescription>Promoted {relativeTime(endpoint.promoted_at)}{endpoint.overridden ? " by an audited override" : ""}.</CardDescription>
              </div>
              <GateBadge outcome={endpoint.gate.outcome} overridden={Boolean(endpoint.overridden)} />
            </CardHeader>
            <CardContent className="grid gap-4">
              <KeyValues
                items={[
                  [
                    "Model version",
                    <Link key="mv" to="/models/$modelId/versions/$version" params={{ modelId: endpoint.model_id, version: String(endpoint.model_ref.split("@v")[1] ?? "1") }} className="text-primary hover:underline">
                      <Mono>{endpoint.model_ref}</Mono>
                    </Link>,
                  ],
                  ["Promoted by", endpoint.promoted_by ? <PersonName key="by" id={endpoint.promoted_by} /> : "—"],
                  ["Scorecard", endpoint.gate.scorecard_overall ?? "none"],
                  ["Accepted fits", endpoint.evaluation_policy?.fit_mode === "train_only" ? "Exploration fits too (train only)" : "Evaluated, then refitted on all data"],
                  ["Input changes", { block: "Blocked", warn: "Need an override", allow: "Allowed" }[endpoint.evaluation_policy?.schema_change ?? "block"]],
                ]}
              />
              <GateReasons decision={endpoint.gate} empty="The version passed every check when it was promoted." />
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <div>
                <CardTitle>History</CardTitle>
                <CardDescription>Every promotion and rollback, newest first, with who did it and why.</CardDescription>
              </div>
              <Button variant="ghost" size="sm" disabled={!canDelete} onClick={() => setDeleting(true)}>
                <Trash2 /> Delete endpoint
              </Button>
            </CardHeader>
            <CardContent>
              <HistoryTable endpoint={endpoint} onRollback={setRollback} />
            </CardContent>
          </Card>
        </TabsContent>
        <TabsContent value="schema">
          <EndpointSchemaPanel endpoint={endpoint} />
        </TabsContent>
        <TabsContent value="snippets">
          <EndpointSnippetsPanel endpoint={endpoint} />
        </TabsContent>
      </Tabs>
      {promoting ? <PromoteDialog endpoint={endpoint} onClose={() => setPromoting(false)} onPromoted={() => setPromoting(false)} /> : null}
      {rollback ? <RollbackDialog endpoint={endpoint} {...(rollback === "previous" ? {} : { target: rollback })} onClose={() => setRollback(null)} /> : null}
      {deleting ? <DeleteEndpointDialog endpoint={endpoint} onClose={() => setDeleting(false)} onDeleted={() => void navigate({ to: "/integrations", search: { tab: "endpoints" } })} /> : null}
    </>
  );
}

export function EndpointPage() {
  const { alias } = endpointRoute.useParams();
  const endpoint = useEndpoint(alias);
  return (
    <>
      <BackToHub tab="endpoints" />
      <QueryView query={endpoint} errorTitle="The endpoint could not be loaded">
        {(ep) => <EndpointView endpoint={ep} />}
      </QueryView>
    </>
  );
}

// ----------------------------------------------------------------------- webhook

export function WebhookPage() {
  const { webhookId } = webhookRoute.useParams();
  const webhook = useWebhook(webhookId);
  const navigate = useNavigate();
  const ws = useWorkspace();
  const canDelete = can(ws.grants, "delete", "webhook");
  const [deleting, setDeleting] = useState(false);
  return (
    <>
      <BackToHub tab="webhooks" />
      <QueryView query={webhook} errorTitle="The webhook could not be loaded">
        {(wh) => (
          <>
            <PageHeader
              title={wh.description || "Webhook"}
              meta={
                <>
                  <Mono className="break-all">{wh.url}</Mono>
                  <WebhookStatusBadge webhook={wh} />
                </>
              }
              actions={
                <Button variant="ghost" disabled={!canDelete} onClick={() => setDeleting(true)}>
                  <Trash2 /> Delete
                </Button>
              }
            />
            <div className="grid gap-5">
              <AutoDisabledNotice webhook={wh} />
              <WebhookSettings key={wh.updated_at} webhook={wh} />
              <DeliveryLog webhook={wh} />
            </div>
            {deleting ? <DeleteWebhookDialog webhook={wh} onClose={() => setDeleting(false)} onDeleted={() => void navigate({ to: "/integrations", search: { tab: "webhooks" } })} /> : null}
          </>
        )}
      </QueryView>
    </>
  );
}
