/**
 * The Integrations hub's front page: three Guided cards for the common cases (a Google Sheet,
 * a BI dashboard, a webhook), each finished in place, and the Expert entry points (endpoints,
 * webhooks, sharing, scoped tokens, the API reference).
 */
import { Link } from "@tanstack/react-router";
import { ArrowRight, BarChart3, BookOpen, FileSpreadsheet, KeyRound, Plug, Share2, Webhook as WebhookIcon } from "lucide-react";
import { useState, type ReactNode } from "react";

import { can } from "@/api/access";
import { useEndpoints, useEndpointSnippets, useSavedPlots } from "@/api/integrations";
import type { Endpoint, PlotEmbedCreated, SavedPlot, WebhookCreated } from "@/api/types";
import { LoadingRows, Mono } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle, Field, NativeSelect } from "@/components/ui/primitives";
import { useWorkspace } from "@/features/auth/workspace";
import { env } from "@/env";

import { CodeBlock } from "./endpoint-schema";
import { CreateEmbedDialog, EmbedCreatedDialog } from "./sharing";
import { CreateWebhookDialog, WebhookSecretDialog } from "./webhooks";

export type HubTab = "overview" | "endpoints" | "webhooks" | "sharing";

function GuidedCard({ icon, title, description, children }: { icon: ReactNode; title: string; description: string; children: ReactNode }) {
  return (
    <Card className="flex min-w-0 flex-col">
      <CardHeader>
        <div className="flex min-w-0 items-start gap-3">
          <span aria-hidden className="grid size-9 shrink-0 place-items-center rounded-lg bg-primary/12 text-primary [&_svg]:size-4.5">
            {icon}
          </span>
          <div className="min-w-0">
            <CardTitle>{title}</CardTitle>
            <CardDescription>{description}</CardDescription>
          </div>
        </div>
      </CardHeader>
      <CardContent className="grid flex-1 content-start gap-3">{children}</CardContent>
    </Card>
  );
}

function SheetsSteps({ endpoint }: { endpoint: Endpoint }) {
  const snippets = useEndpointSnippets(endpoint.alias, endpoint.revision);
  const script = snippets.data?.items?.find((s) => s.language === "apps_script");
  return (
    <div className="grid gap-3">
      <ol className="grid list-decimal gap-1 pl-5 text-[13px]">
        <li>
          <Link to="/tokens" search={{ preset: `score:${endpoint.alias}` }} className="text-primary hover:underline">
            Create a token
          </Link>{" "}
          that can only score <Mono>{endpoint.alias}</Mono>.
        </li>
        <li>In the sheet: Extensions → Apps Script, and paste the code below.</li>
        <li>
          Project Settings → Script properties: add <Mono>MAPI_TOKEN</Mono> with the token.
        </li>
        <li>Run the function; the result lands in cell A1.</li>
      </ol>
      {snippets.isPending ? <LoadingRows rows={2} /> : script ? <CodeBlock code={script.code} label="Apps Script" language="apps_script" /> : <p className="text-[13px] text-muted-foreground">No Apps Script snippet for this endpoint.</p>}
    </div>
  );
}

function SheetsCard({ goTo }: { goTo: (tab: HubTab) => void }) {
  const endpoints = useEndpoints();
  const [alias, setAlias] = useState("");
  const items = endpoints.data?.items ?? [];
  const chosen = items.find((e) => e.alias === alias) ?? items[0];
  return (
    <GuidedCard icon={<FileSpreadsheet />} title="Google Sheets" description="Score a model from a spreadsheet with a short Apps Script.">
      {endpoints.isPending ? (
        <LoadingRows rows={2} />
      ) : chosen ? (
        <>
          <Field label="Endpoint" htmlFor="sheets-endpoint">
            <NativeSelect id="sheets-endpoint" value={chosen.alias} onChange={(e) => setAlias(e.target.value)}>
              {items.map((e) => (
                <option key={e.id} value={e.alias}>
                  {e.alias}
                </option>
              ))}
            </NativeSelect>
          </Field>
          <SheetsSteps endpoint={chosen} />
        </>
      ) : (
        <>
          <p className="text-[13px] text-muted-foreground">A sheet calls an endpoint: a stable name for one fitted model. Promote a model first.</p>
          <Button variant="outline" size="sm" className="w-fit" onClick={() => goTo("endpoints")}>
            Set up an endpoint <ArrowRight />
          </Button>
        </>
      )}
    </GuidedCard>
  );
}

function BiCard({ goTo }: { goTo: (tab: HubTab) => void }) {
  const ws = useWorkspace();
  const canEmbed = can(ws.grants, "create", "embed");
  const saved = useSavedPlots();
  const embeddable = (saved.data?.items ?? []).filter((p) => p.embeddable);
  const [plotId, setPlotId] = useState("");
  const chosen = embeddable.find((p) => p.id === plotId) ?? embeddable[0];
  const [sharing, setSharing] = useState<SavedPlot | null>(null);
  const [created, setCreated] = useState<{ c: PlotEmbedCreated; title: string } | null>(null);
  return (
    <GuidedCard icon={<BarChart3 />} title="BI dashboard" description="Put a live, read-only chart into Looker Studio, Power BI, Notion or Slack with an embed link.">
      {saved.isPending ? (
        <LoadingRows rows={2} />
      ) : chosen ? (
        <>
          <Field label="Chart" htmlFor="bi-plot">
            <NativeSelect id="bi-plot" value={chosen.id} onChange={(e) => setPlotId(e.target.value)}>
              {embeddable.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.title}
                </option>
              ))}
            </NativeSelect>
          </Field>
          <p className="text-[13px] text-muted-foreground">The link expires (30 days by default) and can be revoked at any time. Paste it as an embed or iframe URL.</p>
          <div className="flex flex-wrap gap-2">
            <Button size="sm" disabled={!canEmbed} onClick={() => setSharing(chosen)}>
              <Share2 /> Create embed link
            </Button>
            <Button size="sm" variant="ghost" onClick={() => goTo("sharing")}>
              Manage links
            </Button>
          </div>
        </>
      ) : (
        <>
          <p className="text-[13px] text-muted-foreground">Save a plot from a fitted model first; plots of the platform&apos;s recipes can be shared by link.</p>
          <Button variant="outline" size="sm" className="w-fit" onClick={() => goTo("sharing")}>
            Save a model plot <ArrowRight />
          </Button>
        </>
      )}
      <p className="text-xs text-muted-foreground">Need numbers instead of a chart? Score an endpoint from the BI tool&apos;s scheduled script (see the curl or Python snippet).</p>
      {sharing ? (
        <CreateEmbedDialog
          plot={sharing}
          onClose={() => setSharing(null)}
          onCreated={(c) => {
            setCreated({ c, title: sharing.title });
            setSharing(null);
          }}
        />
      ) : null}
      {created ? <EmbedCreatedDialog created={created.c} title={created.title} onClose={() => setCreated(null)} /> : null}
    </GuidedCard>
  );
}

function WebhookCard({ goTo }: { goTo: (tab: HubTab) => void }) {
  const ws = useWorkspace();
  const canCreate = can(ws.grants, "create", "webhook");
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState<WebhookCreated | null>(null);
  return (
    <GuidedCard icon={<WebhookIcon />} title="Webhook" description="Get a signed call in your own system when a model is ready or a run fails.">
      <p className="text-[13px] text-muted-foreground">Point it at a Zapier or Make hook, a Slack workflow or your pipeline. Every delivery is signed, retried and logged.</p>
      <div className="flex flex-wrap gap-2">
        <Button size="sm" disabled={!canCreate} title={canCreate ? undefined : "Project admins set up webhooks"} onClick={() => setCreating(true)}>
          <WebhookIcon /> Set up a webhook
        </Button>
        <Button size="sm" variant="ghost" onClick={() => goTo("webhooks")}>
          Delivery logs
        </Button>
      </div>
      {creating ? (
        <CreateWebhookDialog
          initialEvents={["model.version.ready", "run.failed"]}
          onClose={() => setCreating(false)}
          onCreated={(c) => {
            setCreating(false);
            setCreated(c);
          }}
        />
      ) : null}
      {created ? <WebhookSecretDialog created={created} onClose={() => setCreated(null)} /> : null}
    </GuidedCard>
  );
}

function ExpertLink({ icon, title, children, action }: { icon: ReactNode; title: string; children: ReactNode; action: ReactNode }) {
  return (
    <li className="grid grid-cols-[1.25rem_1fr] gap-3 rounded-lg border p-3 sm:grid-cols-[1.25rem_1fr_auto] sm:items-center">
      <span className="pt-0.5 text-muted-foreground [&_svg]:size-4">{icon}</span>
      <div className="min-w-0">
        <p className="font-medium">{title}</p>
        <p className="text-[13px] text-muted-foreground">{children}</p>
      </div>
      <div className="col-start-2 sm:col-start-3">{action}</div>
    </li>
  );
}

export function IntegrationsOverview({ goTo }: { goTo: (tab: HubTab) => void }) {
  return (
    <div className="grid gap-8">
      <section aria-labelledby="guided-cards" className="grid gap-3">
        <h2 id="guided-cards" className="text-[15px] font-semibold">
          Connect in a few steps
        </h2>
        <div className="grid items-start gap-4 lg:grid-cols-3">
          <SheetsCard goTo={goTo} />
          <BiCard goTo={goTo} />
          <WebhookCard goTo={goTo} />
        </div>
      </section>
      <section aria-labelledby="expert-links" className="grid gap-3">
        <h2 id="expert-links" className="text-[15px] font-semibold">
          For developers
        </h2>
        <ul className="grid gap-2">
          <ExpertLink icon={<Plug />} title="Endpoints and aliases" action={<Button size="sm" variant="outline" onClick={() => goTo("endpoints")}>Open</Button>}>
            Promotion gated on the scorecard, history and rollback, the input contract, Try it, and snippets for Python, TypeScript, curl, R, Apps Script, Atlas and a notebook cell.
          </ExpertLink>
          <ExpertLink
            icon={<KeyRound />}
            title="Scoped tokens"
            action={
              <Button size="sm" variant="outline" asChild>
                <Link to="/tokens">Tokens</Link>
              </Button>
            }
          >
            A <Mono>score:&lt;endpoint&gt;</Mono> token can read and score one endpoint and nothing else; give it a rate limit and a compute budget.
          </ExpertLink>
          <ExpertLink
            icon={<BookOpen />}
            title="API reference"
            action={
              <span className="flex flex-wrap gap-1">
                <Button size="sm" variant="outline" asChild>
                  <a href={`${env.apiBase}/docs`} target="_blank" rel="noopener noreferrer">
                    Interactive docs
                  </a>
                </Button>
                <Button size="sm" variant="ghost" asChild>
                  <a href={`${env.apiBase}/openapi.json`} target="_blank" rel="noopener noreferrer">
                    OpenAPI
                  </a>
                </Button>
              </span>
            }
          >
            Every operation of this server, from its own OpenAPI 3.1 document.
          </ExpertLink>
        </ul>
      </section>
    </div>
  );
}
