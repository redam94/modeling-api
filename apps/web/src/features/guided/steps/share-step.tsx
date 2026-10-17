/**
 * Step 7, "Share": put the model live behind an endpoint (the promotion gate explained before
 * anyone presses the button), create a read-only embed link for a chart, and go on to the
 * Integrations hub (spreadsheets, BI tools, webhooks).
 */
import { Link } from "@tanstack/react-router";
import { ArrowUpRight, CheckCircle2, Code2, KeyRound, Link2, Rocket, ShieldAlert, Sheet as SheetIcon } from "lucide-react";
import { useId, useState } from "react";

import { can } from "@/api/access";
import { ApiError, errorMessage } from "@/api/client";
import type { ModelVersion } from "@/api/types";
import { CopyButton, Mono, Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Badge, Card, CardContent, CardDescription, CardHeader, CardTitle, Field, Input, NativeSelect, Textarea } from "@/components/ui/primitives";
import { useGrants } from "@/features/auth/workspace";

import { useCreateEmbed, useEndpoints, usePromoteEndpoint } from "../api";
import { StepActions } from "../frame";
import { gateSentence, previewGate } from "../gate";
import { useModelPlots } from "../model-plots";
import { slugify, valueAt } from "../session";
import type { Endpoint, PlotEmbedCreated } from "../types";
import { StatusBadge } from "../ui";
import type { GuidedSession } from "../use-session";

const PLATFORM_NAMESPACES = ["core", "mmm", "ml", "ts", "choice"];

export function isPlatformRecipe(recipe: string | null | undefined): boolean {
  if (!recipe) return false;
  const [ns, rest] = recipe.split("@", 1)[0]!.split(/\.(.+)/);
  return Boolean(ns && rest && PLATFORM_NAMESPACES.includes(ns));
}

function GateReasons({ reasons }: { reasons: { message_plain: string; outcome: string }[] }) {
  if (!reasons.length) return null;
  return (
    <ul className="grid gap-2">
      {reasons.map((r, i) => (
        <li key={i} className="flex items-start gap-2 text-[13px]">
          <StatusBadge status={r.outcome === "blocked" ? "fail" : "warn"} className="mt-0.5" />
          <span>{r.message_plain}</span>
        </li>
      ))}
    </ul>
  );
}

function Promote({ session, mv }: { session: GuidedSession; mv: ModelVersion }) {
  const id = useId();
  const grants = useGrants();
  const endpoints = useEndpoints();
  const promote = usePromoteEndpoint();
  const [alias, setAlias] = useState(slugify(`${session.spec.name}`));
  const [reason, setReason] = useState("");
  const [done, setDone] = useState<Endpoint | null>(null);
  const [error, setError] = useState<unknown>(null);
  const fitMode = valueAt(session.document, "/model/fit/mode");
  const gate = previewGate(mv.scorecard, { partial: mv.status === "partial", fitMode: typeof fitMode === "string" ? fitMode : null, requiredChecks: session.template.scorecard_checks ?? [] });
  const isAdmin = can(grants, "manage", "endpoint");
  const mayPromote = can(grants, "promote", "endpoint");
  const existing = endpoints.data?.items.find((e) => e.alias === alias) ?? null;
  const already = existing?.model_version_id === mv.id;
  const aliasOk = /^[a-z0-9](?:[a-z0-9_-]{0,126}[a-z0-9])?$/.test(alias);
  const problemReasons = error instanceof ApiError ? (error.problem?.errors ?? []).map((e) => ({ message_plain: e.message, outcome: e.severity === "error" ? "blocked" : "needs_override" })) : [];

  const submit = async (override: boolean) => {
    setError(null);
    try {
      const ep = await promote.mutateAsync({
        alias,
        body: {
          model_version_id: mv.id,
          expected_revision: existing?.revision ?? 0,
          description: `${session.template.title}: ${session.spec.name}`.slice(0, 2000),
          ...(override ? { override: true, reason: reason.trim() } : {}),
        },
      });
      setDone(ep);
    } catch (e) {
      setError(e);
    }
  };

  const live = done ?? (already ? existing : null);
  return (
    <Card>
      <CardHeader>
        <div className="grid gap-1">
          <CardTitle>
            <span className="inline-flex items-center gap-2">
              <Rocket aria-hidden className="size-4 text-muted-foreground" /> Put the model live
            </span>
          </CardTitle>
          <CardDescription>An endpoint gives other tools a stable address to send questions to this model. For example, a planning spreadsheet, a dashboard or a script can use it. Later, you can replace the model behind the endpoint. The tools continue to work.</CardDescription>
        </div>
      </CardHeader>
      <CardContent className="grid gap-4">
        {live ? (
          <div className="grid gap-2 rounded-lg border border-success/30 bg-success/6 p-4" data-testid="endpoint-live">
            <p className="flex items-center gap-2 font-medium">
              <CheckCircle2 aria-hidden className="size-4 text-success" /> Live at <Mono>{live.ref}</Mono>
            </p>
            <p className="text-[13px] text-muted-foreground">
              The endpoint serves {live.model_ref} (revision {live.revision}).{live.overridden ? " An admin approved it, but some checks need examination." : ""} The Integrations hub has ready-made code for spreadsheets, notebooks and scripts.
            </p>
            <div className="flex flex-wrap gap-2 pt-1">
              <Button asChild size="sm" variant="outline">
                <Link to="/integrations/endpoints/$alias" params={{ alias: live.alias }} search={{ tab: "snippets" }}>
                  <Code2 /> Code for this endpoint
                </Link>
              </Button>
              <Button asChild size="sm" variant="ghost">
                <Link to="/tokens" search={{ preset: `score:${live.alias}` }}>
                  <KeyRound /> Create a token for it
                </Link>
              </Button>
            </div>
          </div>
        ) : (
          <>
            <div className={`flex items-start gap-3 rounded-lg border p-4 ${gate.outcome === "allowed" ? "border-success/30" : gate.outcome === "blocked" ? "border-destructive/30 bg-destructive/6" : "border-warning/35 bg-warning/8"}`}>
              {gate.outcome === "allowed" ? <CheckCircle2 aria-hidden className="mt-0.5 size-4 shrink-0 text-success" /> : <ShieldAlert aria-hidden className={`mt-0.5 size-4 shrink-0 ${gate.outcome === "blocked" ? "text-destructive" : "text-warning"}`} />}
              <div className="grid gap-2">
                <p className="text-[13px] font-medium" data-testid="gate-sentence">
                  {gateSentence(gate.outcome, isAdmin)}
                </p>
                <GateReasons reasons={gate.reasons ?? []} />
                {gate.outcome === "blocked" ? (
                  <Link to="/guided/$specId/$step" params={{ specId: session.spec.id, step: "report" }} className="text-[13px] font-medium underline underline-offset-2">
                    See what to do on the Results step
                  </Link>
                ) : null}
              </div>
            </div>
            <Field label="Endpoint name" htmlFor={`${id}-alias`} hint={existing ? `This name is in use. It serves ${existing.model_ref}. If you promote this model, it replaces that model.` : "Use lower-case letters, digits and dashes. Tools use this name to find the model."} error={aliasOk ? undefined : "Use lower-case letters, digits and dashes."}>
              <Input id={`${id}-alias`} value={alias} onChange={(e) => setAlias(e.target.value)} className="max-w-sm font-mono" />
            </Field>
            {gate.outcome === "needs_override" && isAdmin ? (
              <Field label="Why is it fine to go live anyway?" htmlFor={`${id}-reason`} hint="The audit log keeps this reason with your name.">
                <Textarea id={`${id}-reason`} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Holdout error is high because of a one-off promotion in the last weeks." maxLength={1000} />
              </Field>
            ) : null}
            {error ? (
              <div role="alert" className="grid gap-2 rounded-lg border border-destructive/35 bg-destructive/6 p-4 text-[13px]">
                <p className="font-medium">{error instanceof ApiError && error.code === "OVERRIDE_REQUIRED" ? "A project admin must approve this model" : error instanceof ApiError && error.code === "PROMOTION_BLOCKED" ? "This model cannot go live" : "The model did not go live"}</p>
                {problemReasons.length ? <GateReasons reasons={problemReasons} /> : <p className="text-muted-foreground">{errorMessage(error)}</p>}
              </div>
            ) : null}
            <div className="flex flex-wrap items-center gap-3">
              {gate.outcome === "needs_override" ? (
                isAdmin ? (
                  <Button onClick={() => void submit(true)} disabled={!aliasOk || !reason.trim() || promote.isPending}>
                    {promote.isPending ? <Spinner /> : <Rocket />} Approve and go live
                  </Button>
                ) : (
                  <p className="text-[13px] text-muted-foreground">Send this page to a project admin. A project admin can approve the model here.</p>
                )
              ) : (
                <Button onClick={() => void submit(false)} disabled={!aliasOk || gate.outcome === "blocked" || promote.isPending || !mayPromote}>
                  {promote.isPending ? <Spinner /> : <Rocket />} Go live
                </Button>
              )}
              {!mayPromote ? <span className="text-xs text-muted-foreground">You need the admin role to put a model live.</span> : null}
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}

/** Charts people share first: results before the sampler's diagnostics (offered last). */
const SHARE_FIRST = ["mmm.response_curves", "mmm.decomposition", "core.ppc_fan", "core.fit_vs_actual"];

export function shareOrder<T extends { id: string }>(plots: T[]): T[] {
  const rank = (p: T) => {
    const i = SHARE_FIRST.indexOf(p.id);
    return i >= 0 ? i : p.id.startsWith("core.convergence") ? SHARE_FIRST.length + 1 : SHARE_FIRST.length;
  };
  return [...plots].sort((a, b) => rank(a) - rank(b));
}

function Embed({ mv }: { mv: ModelVersion }) {
  const id = useId();
  const grants = useGrants();
  const plots = useModelPlots(mv);
  const embeddable = shareOrder((plots.data ?? []).filter((p) => isPlatformRecipe(p.recipe)));
  const [plotId, setPlotId] = useState<string>("");
  const [days, setDays] = useState(30);
  const create = useCreateEmbed();
  const [made, setMade] = useState<PlotEmbedCreated | null>(null);
  const chosen = embeddable.find((p) => p.id === plotId) ?? embeddable[0];
  return (
    <Card>
      <CardHeader>
        <div className="grid gap-1">
          <CardTitle>
            <span className="inline-flex items-center gap-2">
              <Link2 aria-hidden className="size-4 text-muted-foreground" /> Share a chart
            </span>
          </CardTitle>
          <CardDescription>Make a read-only link to one chart for a slide, a Notion page or a dashboard. All persons with the link can see the chart until the link expires. The link shares nothing else.</CardDescription>
        </div>
      </CardHeader>
      <CardContent className="grid gap-4">
        {made ? (
          <div className="grid gap-2 rounded-lg border bg-muted/30 p-4" data-testid="embed-link">
            <p className="text-[13px] font-medium">Your link. Copy it now. The app shows it only one time.</p>
            <div className="flex min-w-0 items-center gap-2">
              <Mono className="min-w-0 flex-1 truncate rounded border bg-card px-2 py-1.5">{made.url}</Mono>
              <CopyButton value={made.url} label="Copy the link" />
            </div>
            <p className="text-xs text-muted-foreground">
              The link expires on {new Date(made.embed.expires_at).toLocaleDateString("en", { dateStyle: "medium" })}. You can cancel it under{" "}
              <Link to="/integrations" search={{ tab: "sharing" }} className="underline underline-offset-2">
                Plots and links
              </Link>{" "}
              in the Integrations hub.
            </p>
          </div>
        ) : embeddable.length ? (
          <div className="flex flex-wrap items-end gap-3">
            <Field label="Chart" htmlFor={`${id}-plot`}>
              <NativeSelect id={`${id}-plot`} value={chosen?.id ?? ""} onChange={(e) => setPlotId(e.target.value)} className="w-64">
                {embeddable.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.title}
                  </option>
                ))}
              </NativeSelect>
            </Field>
            <Field label="Link works for" htmlFor={`${id}-days`}>
              <NativeSelect id={`${id}-days`} value={days} onChange={(e) => setDays(Number(e.target.value))} className="w-36">
                <option value={7}>7 days</option>
                <option value={30}>30 days</option>
                <option value={90}>90 days</option>
                <option value={365}>a year</option>
              </NativeSelect>
            </Field>
            <Button
              variant="outline"
              disabled={!chosen || create.isPending || !can(grants, "create", "embed")}
              onClick={() =>
                chosen &&
                create.mutate(
                  { plot: { title: `${chosen.title} (${mv.ref})`.slice(0, 300), source: { model_id: mv.model_id, version: mv.version, plot_id: chosen.id } }, embed: { expires_in_days: days, label: "Guided share" } },
                  { onSuccess: (r) => setMade(r.created) },
                )
              }
            >
              {create.isPending ? <Spinner /> : <Link2 />} Create link
            </Button>
          </div>
        ) : (
          <p className="text-[13px] text-muted-foreground">This model has no charts that you can share as a link.</p>
        )}
        {create.isError ? (
          <p role="alert" className="text-[13px] text-destructive">
            {errorMessage(create.error)}
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}

export function ShareStep({ session }: { session: GuidedSession }) {
  const mv = session.modelVersion;
  if (!mv) return <p className="text-[13px] text-muted-foreground">This analysis has no fitted model. Fit the model to share it.</p>;
  return (
    <div className="grid gap-6">
      <Promote session={session} mv={mv} />
      <Embed mv={mv} />
      <Card>
        <CardHeader>
          <div className="grid gap-1">
            <CardTitle>
              <span className="inline-flex items-center gap-2">
                <SheetIcon aria-hidden className="size-4 text-muted-foreground" /> Connect other tools
              </span>
            </CardTitle>
            <CardDescription>Connect Google Sheets and BI dashboards. Set up scheduled exports and notifications when a fit is complete.</CardDescription>
          </div>
          <Badge variant="secondary">Integrations</Badge>
        </CardHeader>
        <CardContent>
          <Button asChild variant="outline">
            <Link to="/integrations" search={{ tab: "overview" }}>
              Open the Integrations hub <ArrowUpRight />
            </Link>
          </Button>
        </CardContent>
      </Card>
      <StepActions>
        <span />
        <Button asChild variant="ghost">
          <Link to="/guided">Back to all analyses</Link>
        </Button>
      </StepActions>
    </div>
  );
}
